#!/usr/bin/env python3
"""Sample CPU% and memory for a process tree while a command runs.

Hyperlight guest RAM is backed by the host `pyhl` process (and related
threads/children). This monitor records that host-side cost over time —
the analogue of agentcgroup's podman-stats sampling for containers.

Usage:
  python3 monitor_resources.py --out results/resources.json -- \\
      cargo run --manifest-path ../../host/Cargo.toml --bin pyhl -- run ...

  # Also track an already-running helper (e.g. bash proxy):
  python3 monitor_resources.py --also-pid 1234 --label-also bash_proxy --out r.json -- ./pyhl ...
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path


def _read_stat(pid: int) -> tuple[int, int] | None:
    """Return (utime+stime jiffies, rss_pages) or None if gone."""
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as f:
            st = f.read()
        # comm may contain spaces/parens; split after last ')'
        rparen = st.rfind(")")
        if rparen < 0:
            return None
        # After ')': state ppid ... utime(11) stime(12) ... rss(21)
        fields = st[rparen + 2 :].split()
        utime = int(fields[11])
        stime = int(fields[12])
        rss = int(fields[21])
        return utime + stime, rss
    except (FileNotFoundError, ProcessLookupError, IndexError, ValueError, OSError):
        return None


def _children(pid: int) -> list[int]:
    try:
        with open(f"/proc/{pid}/task/{pid}/children", encoding="utf-8") as f:
            return [int(x) for x in f.read().split() if x.isdigit()]
    except (FileNotFoundError, ProcessLookupError, OSError):
        return []


def _descendants(root: int) -> set[int]:
    seen: set[int] = set()
    stack = [root]
    while stack:
        p = stack.pop()
        if p in seen:
            continue
        seen.add(p)
        stack.extend(_children(p))
    return seen


def _cmdline(pid: int) -> str:
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            raw = f.read().replace(b"\x00", b" ").decode("utf-8", "replace").strip()
        return raw[:200]
    except OSError:
        return ""


def _sample_tree(root: int, prev: dict[int, int] | None, dt: float, page_size: int) -> dict:
    """Aggregate CPU% and RSS for root + descendants."""
    pids = _descendants(root)
    rss_pages = 0
    total_delta = 0
    new_prev: dict[int, int] = {}
    n_proc = 0
    for pid in pids:
        st = _read_stat(pid)
        if st is None:
            continue
        jiffies, rss = st
        n_proc += 1
        rss_pages += rss
        new_prev[pid] = jiffies
        if prev is not None and pid in prev and dt > 0:
            total_delta += max(0, jiffies - prev[pid])
    # CPU% = jiffies_delta / (HZ * dt) * 100; HZ usually 100
    try:
        hz = os.sysconf("SC_CLK_TCK")
    except (AttributeError, ValueError, OSError):
        hz = 100
    cpu_pct = (total_delta / (hz * dt)) * 100.0 if prev is not None and dt > 0 else 0.0
    rss_bytes = rss_pages * page_size
    return {
        "pids": sorted(new_prev.keys()),
        "n_processes": n_proc,
        "cpu_percent": round(cpu_pct, 2),
        "rss_bytes": rss_bytes,
        "rss_mb": round(rss_bytes / (1024 * 1024), 2),
        "_prev": new_prev,
    }


class Sampler(threading.Thread):
    def __init__(self, targets: list[tuple[str, int]], interval: float):
        super().__init__(daemon=True)
        self.targets = targets  # (label, pid)
        self.interval = interval
        self.samples: list[dict] = []
        self._stop_event = threading.Event()
        try:
            self.page_size = os.sysconf("SC_PAGE_SIZE")
        except (AttributeError, ValueError, OSError):
            self.page_size = 4096

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        prevs: dict[str, dict[int, int]] = {}
        t0 = time.time()
        last = t0
        # Prime previous jiffies
        for label, pid in self.targets:
            s = _sample_tree(pid, None, 0.0, self.page_size)
            prevs[label] = s["_prev"]
        while not self._stop_event.wait(self.interval):
            now = time.time()
            dt = now - last
            last = now
            row: dict = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "elapsed_s": round(now - t0, 3),
            }
            for label, pid in self.targets:
                if not Path(f"/proc/{pid}").exists():
                    row[label] = {"alive": False}
                    continue
                s = _sample_tree(pid, prevs.get(label), dt, self.page_size)
                prevs[label] = s.pop("_prev")
                s["alive"] = True
                s["root_pid"] = pid
                s["cmdline"] = _cmdline(pid)
                row[label] = s
            self.samples.append(row)


def _summarize(samples: list[dict], label: str) -> dict:
    cpus = []
    rss = []
    for s in samples:
        block = s.get(label) or {}
        if not block.get("alive"):
            continue
        cpus.append(float(block.get("cpu_percent") or 0))
        rss.append(float(block.get("rss_mb") or 0))
    if not cpus:
        return {"samples": 0}
    return {
        "samples": len(cpus),
        "cpu_percent": {
            "min": round(min(cpus), 2),
            "max": round(max(cpus), 2),
            "avg": round(statistics.mean(cpus), 2),
        },
        "rss_mb": {
            "min": round(min(rss), 2),
            "max": round(max(rss), 2),
            "avg": round(statistics.mean(rss), 2),
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True, help="Output JSON path")
    parser.add_argument("--interval", type=float, default=0.5, help="Sample interval seconds")
    parser.add_argument("--also-pid", type=int, action="append", default=[], help="Extra PIDs to track")
    parser.add_argument(
        "--label-also",
        action="append",
        default=[],
        help="Labels for --also-pid (same order); default also_0, also_1, ...",
    )
    parser.add_argument(
        "--label-main",
        default="hyperlight",
        help="Label for the main command process tree (default: hyperlight)",
    )
    parser.add_argument("command", nargs=argparse.REMAINDER, help="Command after --")
    args = parser.parse_args()

    cmd = list(args.command)
    # argparse.REMAINDER keeps a leading "--" when the caller uses "--" to end options.
    while cmd and cmd[0] == "--":
        cmd = cmd[1:]
    if not cmd:
        print("usage: monitor_resources.py --out FILE -- COMMAND...", file=sys.stderr)
        return 2

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    proc = subprocess.Popen(cmd)
    targets: list[tuple[str, int]] = [(args.label_main, proc.pid)]
    for i, pid in enumerate(args.also_pid):
        label = args.label_also[i] if i < len(args.label_also) else f"also_{i}"
        targets.append((label, pid))

    sampler = Sampler(targets, interval=args.interval)
    sampler.start()
    print(
        f"[monitor] tracking {targets} every {args.interval}s → {out_path}",
        flush=True,
    )
    try:
        rc = proc.wait()
    finally:
        sampler.stop()
        sampler.join(timeout=2.0)

    summary = {label: _summarize(sampler.samples, label) for label, _ in targets}
    payload = {
        "command": cmd,
        "main_pid": proc.pid,
        "exit_code": rc,
        "interval_s": args.interval,
        "note": (
            "RSS/CPU are host-side for the process tree. Hyperlight guest memory "
            "is included in the hyperlight/pyhl RSS (sandbox heap + VM)."
        ),
        "summary": summary,
        "samples": sampler.samples,
    }
    out_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    print(f"[monitor] wrote {out_path} ({len(sampler.samples)} samples)")
    for label, s in summary.items():
        if s.get("samples", 0) == 0:
            print(f"[monitor] {label}: no samples")
            continue
        print(
            f"[monitor] {label}: "
            f"CPU% avg={s['cpu_percent']['avg']} max={s['cpu_percent']['max']} | "
            f"RSS_MiB avg={s['rss_mb']['avg']} max={s['rss_mb']['max']}"
        )
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
