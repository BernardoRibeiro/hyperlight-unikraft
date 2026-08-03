#!/usr/bin/env python3
"""Host-side bash executor for the Hyperlight guest `bash` tool.

Protocol (hostfs has no atomic rename):
  1. Guest writes workspace/.bash_rpc/pending/<id>.req  (JSON body)
  2. Guest writes workspace/.bash_rpc/pending/<id>.ready (marker)
  3. This process reads .req only after .ready exists, runs bash, writes
     workspace/.bash_rpc/done/<id>.json, then deletes .req/.ready.

Usage:
  python3 host_bash_proxy.py --workspace workspace
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

DEFAULT_TIMEOUT = 120
MAX_TIMEOUT = 600
MAX_OUTPUT = 200_000


def _workspace_path(workspace: Path, guest_cwd: str) -> Path:
    raw = (guest_cwd or "testbed").strip() or "testbed"
    if raw.startswith("/host"):
        rel = raw[len("/host") :].lstrip("/") or "."
    elif raw.startswith("/"):
        raise ValueError(f"cwd must be under /host, got {guest_cwd!r}")
    else:
        rel = raw
    resolved = (workspace / rel).resolve()
    workspace_real = workspace.resolve()
    if resolved != workspace_real and not str(resolved).startswith(str(workspace_real) + os.sep):
        raise ValueError(f"cwd escapes workspace: {guest_cwd!r}")
    if not resolved.is_dir():
        raise ValueError(f"cwd is not a directory: {resolved}")
    return resolved


def _truncate(s: str, limit: int = MAX_OUTPUT) -> str:
    if len(s) <= limit:
        return s
    return s[:limit] + f"\n...[truncated {len(s) - limit} chars]..."


def _read_json_stable(path: Path, attempts: int = 10, delay: float = 0.05) -> dict:
    """Read JSON, retrying briefly if the file is empty/partial."""
    last_exc: Exception | None = None
    for _ in range(attempts):
        try:
            text = path.read_text(encoding="utf-8")
            if not text.strip():
                time.sleep(delay)
                continue
            data = json.loads(text)
            if isinstance(data, dict):
                return data
            raise ValueError("request JSON must be an object")
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            last_exc = exc
            time.sleep(delay)
    raise ValueError(f"incomplete or invalid request after retries: {last_exc}")


def _handle_one(ready_path: Path, done_dir: Path, workspace: Path) -> None:
    req_id = ready_path.stem
    req_path = ready_path.with_suffix(".req")
    if not req_path.is_file():
        # Marker without body yet — wait for the next poll cycle.
        return

    try:
        data = _read_json_stable(req_path)
    except Exception as exc:
        print(f"[bash-proxy] bad request {req_path.name}: {exc}", file=sys.stderr)
        # Only delete after retries failed (not a transient empty read).
        ready_path.unlink(missing_ok=True)
        req_path.unlink(missing_ok=True)
        _write_done(
            done_dir,
            req_id,
            {
                "id": req_id,
                "exit_code": 1,
                "stdout": "",
                "stderr": f"Error: bad bash request: {exc}",
            },
        )
        return

    req_id = str(data.get("id") or req_id)
    command = data.get("command")
    if not isinstance(command, str) or not command.strip():
        _write_done(
            done_dir,
            req_id,
            {
                "id": req_id,
                "exit_code": 1,
                "stdout": "",
                "stderr": "Error: command must be a non-empty string",
            },
        )
        ready_path.unlink(missing_ok=True)
        req_path.unlink(missing_ok=True)
        return

    try:
        cwd = _workspace_path(workspace, str(data.get("cwd") or "testbed"))
        timeout = int(data.get("timeout_sec") or DEFAULT_TIMEOUT)
        timeout = max(1, min(timeout, MAX_TIMEOUT))
    except (ValueError, TypeError) as exc:
        _write_done(
            done_dir,
            req_id,
            {"id": req_id, "exit_code": 1, "stdout": "", "stderr": f"Error: {exc}"},
        )
        ready_path.unlink(missing_ok=True)
        req_path.unlink(missing_ok=True)
        return

    env = os.environ.copy()
    path_prefix: list[str] = []
    for candidate in (
        cwd / ".venv" / "bin",
        workspace / "testbed" / ".venv" / "bin",
        workspace / ".venv" / "bin",
    ):
        if candidate.is_dir():
            path_prefix.append(str(candidate))
            break
    if path_prefix:
        env["PATH"] = os.pathsep.join(path_prefix + [env.get("PATH", "")])
        env["VIRTUAL_ENV"] = str(Path(path_prefix[0]).parent)

    print(
        f"[bash-proxy] id={req_id} cwd={cwd} timeout={timeout}s "
        f"venv={env.get('VIRTUAL_ENV', '-')} cmd={command!r}"
    )
    try:
        proc = subprocess.run(
            ["bash", "-lc", command],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
        result = {
            "id": req_id,
            "exit_code": proc.returncode,
            "stdout": _truncate(proc.stdout or ""),
            "stderr": _truncate(proc.stderr or ""),
        }
    except subprocess.TimeoutExpired:
        result = {
            "id": req_id,
            "exit_code": 124,
            "stdout": "",
            "stderr": f"Error: command timed out after {timeout}s",
        }
    except Exception as exc:
        result = {
            "id": req_id,
            "exit_code": 1,
            "stdout": "",
            "stderr": f"Error running command: {exc}",
        }

    _write_done(done_dir, req_id, result)
    ready_path.unlink(missing_ok=True)
    req_path.unlink(missing_ok=True)
    print(f"[bash-proxy] id={req_id} exit={result['exit_code']}")


def _write_done(done_dir: Path, req_id: str, result: dict) -> None:
    done_dir.mkdir(parents=True, exist_ok=True)
    out = done_dir / f"{req_id}.json"
    # Host-local FS supports replace; write atomically from the host side.
    tmp = done_dir / f"{req_id}.json.tmp"
    tmp.write_text(json.dumps(result), encoding="utf-8")
    tmp.replace(out)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", default="workspace", help="Host mount directory")
    parser.add_argument("--poll", type=float, default=0.1, help="Poll interval seconds")
    args = parser.parse_args()

    workspace = Path(args.workspace).resolve()
    pending = workspace / ".bash_rpc" / "pending"
    done = workspace / ".bash_rpc" / "done"
    pending.mkdir(parents=True, exist_ok=True)
    done.mkdir(parents=True, exist_ok=True)

    # Clear stale requests from a previous crashed run.
    for pattern in ("*.ready", "*.req", "*.json", "*.tmp"):
        for stale in pending.glob(pattern):
            print(f"[bash-proxy] dropping stale {stale.name}")
            stale.unlink(missing_ok=True)

    print(f"[bash-proxy] watching {pending} for *.ready (workspace={workspace})")
    try:
        while True:
            for ready in sorted(pending.glob("*.ready")):
                _handle_one(ready, done, workspace)
            time.sleep(args.poll)
    except KeyboardInterrupt:
        print("[bash-proxy] stopped")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
