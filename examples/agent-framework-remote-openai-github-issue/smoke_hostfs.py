#!/usr/bin/env python3
"""Phase 2 smoke test: verify the guest can read/write the host mount at /host.

No network and no OpenAI key. Expects Phase 1 layout:
  /host/issue.md
  /host/testbed/
"""

import os
import sys

HOST = "/host"
ISSUE = os.path.join(HOST, "issue.md")
TESTBED = os.path.join(HOST, "testbed")
MARKER = os.path.join(HOST, ".hyperlight_smoke_ok")


def main() -> int:
    print(f"host mount present: {os.path.isdir(HOST)}")
    if not os.path.isdir(HOST):
        print("FAIL: /host is not mounted", file=sys.stderr)
        return 1

    if not os.path.isfile(ISSUE):
        print(f"FAIL: missing {ISSUE}", file=sys.stderr)
        return 1
    with open(ISSUE, encoding="utf-8", errors="replace") as f:
        issue = f.read()
    print(f"read {ISSUE}: {len(issue)} bytes")
    print(f"issue head: {issue.strip().splitlines()[0][:80]!r}")

    if not os.path.isdir(TESTBED):
        print(f"FAIL: missing {TESTBED}", file=sys.stderr)
        return 1
    entries = sorted(os.listdir(TESTBED))[:12]
    print(f"list {TESTBED}: {entries!r}{'...' if len(os.listdir(TESTBED)) > 12 else ''}")

    plugin = os.path.join(TESTBED, "pyupgrade", "_plugins", "shlex_join.py")
    tests = os.path.join(TESTBED, "tests", "features", "shlex_join_test.py")
    for path in (plugin, tests):
        ok = os.path.isfile(path)
        print(f"{'ok' if ok else 'MISSING'}: {path}")
        if not ok:
            return 1

    with open(MARKER, "w", encoding="utf-8") as f:
        f.write("hostfs smoke ok\n")
    with open(MARKER, encoding="utf-8") as f:
        got = f.read().strip()
    print(f"wrote+read {MARKER}: {got!r}")
    if got != "hostfs smoke ok":
        print("FAIL: marker round-trip mismatch", file=sys.stderr)
        return 1

    print("SMOKE OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
