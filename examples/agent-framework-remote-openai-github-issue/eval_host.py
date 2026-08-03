#!/usr/bin/env python3
"""Phase 5: host-side evaluation of the Hyperlight SWE agent result.

Runs outside the unikernel against workspace/testbed:
  - focused pytest (shlex_join)
  - optional full pytest
  - git diff capture
  - semantic checks for CLI_Tools_Easy / asottile__pyupgrade-939

Usage:
  python3 eval_host.py
  python3 eval_host.py --full-suite
  python3 eval_host.py --workspace workspace --out results/eval
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

TASK = {
    "name": "CLI_Tools_Easy",
    "instance_id": "asottile__pyupgrade-939",
    "plugin": "pyupgrade/_plugins/shlex_join.py",
    "focused_tests": "tests/features/shlex_join_test.py",
}


def _run(cmd: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=600,
    )


def _check_fix_present(plugin: Path) -> tuple[bool, str]:
    text = plugin.read_text(encoding="utf-8")
    ok = "node.func.value.value == ' '" in text or 'node.func.value.value == " "' in text
    detail = "found node.func.value.value == ' ' guard" if ok else "missing space-separator guard"
    return ok, detail


def _reproduce(python: Path, testbed: Path) -> tuple[bool, str]:
    """True if non-space join is NOT rewritten to shlex.join (bug fixed)."""
    code = r"""
from pyupgrade._main import _fix_plugins
from pyupgrade._data import Settings
s = 'import shlex\ntrash_bin = "garbage".join(shlex.quote(a) for a in ["some", "quotable strings"])\n'
out = _fix_plugins(s, settings=Settings(min_version=(3, 8)))
print(out)
# Fixed => still has "garbage".join and was not rewritten to shlex.join(...)
ok = '"garbage".join' in out and "trash_bin = shlex.join" not in out
raise SystemExit(0 if ok else 1)
"""
    proc = _run([str(python), "-c", code], cwd=testbed)
    fixed = proc.returncode == 0
    detail = (proc.stdout or "") + (proc.stderr or "")
    return fixed, detail.strip()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", default="workspace")
    parser.add_argument("--out", default="", help="Output dir (default results/eval_<timestamp>)")
    parser.add_argument("--full-suite", action="store_true", help="Also run full pytest suite")
    args = parser.parse_args()

    workspace = Path(args.workspace).resolve()
    testbed = workspace / "testbed"
    python = testbed / ".venv" / "bin" / "python"
    if not testbed.is_dir():
        print(f"FAIL: missing {testbed}", file=sys.stderr)
        return 1
    if not python.is_file():
        print("FAIL: missing testbed venv — run: just deps", file=sys.stderr)
        return 1

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_dir = Path(args.out) if args.out else Path("results") / f"eval_{ts}"
    out_dir.mkdir(parents=True, exist_ok=True)

    report: dict = {
        "task": TASK,
        "timestamp_utc": ts,
        "workspace": str(workspace),
        "testbed": str(testbed),
        "checks": {},
    }

    # 1) git status / diff
    status = _run(["git", "status", "--short"], cwd=testbed)
    diff = _run(["git", "diff", "--", TASK["plugin"], TASK["focused_tests"]], cwd=testbed)
    (out_dir / "git_status.txt").write_text(status.stdout + status.stderr, encoding="utf-8")
    (out_dir / "git_diff.patch").write_text(diff.stdout, encoding="utf-8")
    report["checks"]["git_diff_nonempty"] = {
        "pass": bool(diff.stdout.strip()),
        "detail": f"{len(diff.stdout)} bytes in focused diff",
    }

    # 2) plugin contains the known fix
    plugin_path = testbed / TASK["plugin"]
    fix_ok, fix_detail = _check_fix_present(plugin_path)
    report["checks"]["space_separator_guard"] = {"pass": fix_ok, "detail": fix_detail}

    # 3) reproduce issue example (must remain untransformed)
    repro_ok, repro_detail = _reproduce(python, testbed)
    (out_dir / "reproduce.txt").write_text(repro_detail + "\n", encoding="utf-8")
    report["checks"]["reproduce_garbage_join_noop"] = {
        "pass": repro_ok,
        "detail": repro_detail.splitlines()[-1] if repro_detail else "",
    }

    # 4) focused pytest
    focused = _run(
        [str(python), "-m", "pytest", "-q", TASK["focused_tests"]],
        cwd=testbed,
    )
    (out_dir / "pytest_focused.txt").write_text(
        focused.stdout + focused.stderr, encoding="utf-8"
    )
    report["checks"]["pytest_focused"] = {
        "pass": focused.returncode == 0,
        "exit_code": focused.returncode,
        "detail": (focused.stdout or focused.stderr).strip().splitlines()[-1:]
        or [""],
    }

    # 5) optional full suite
    if args.full_suite:
        full = _run([str(python), "-m", "pytest", "-q"], cwd=testbed)
        (out_dir / "pytest_full.txt").write_text(full.stdout + full.stderr, encoding="utf-8")
        report["checks"]["pytest_full"] = {
            "pass": full.returncode == 0,
            "exit_code": full.returncode,
            "detail": (full.stdout or full.stderr).strip().splitlines()[-1:] or [""],
        }

    passed = all(c.get("pass") for c in report["checks"].values())
    report["overall_pass"] = passed

    (out_dir / "results.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    print(f"Phase 5 evaluation → {out_dir}")
    for name, check in report["checks"].items():
        mark = "PASS" if check.get("pass") else "FAIL"
        detail = check.get("detail")
        if isinstance(detail, list):
            detail = detail[0] if detail else ""
        print(f"  [{mark}] {name}: {detail}")
    print(f"OVERALL: {'PASS' if passed else 'FAIL'}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
