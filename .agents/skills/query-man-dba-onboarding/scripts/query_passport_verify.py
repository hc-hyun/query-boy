#!/usr/bin/env python3
"""DBA Execute entrypoint; authorization remains with the skill and executor."""

import os
import runpy
import sys
from pathlib import Path


def main() -> int:
    try:
        shared = Path(__file__).parents[2] / "query-man-admin/scripts/query_passport.py"
        consumer = runpy.run_path(str(shared))
        return int(consumer["run"](["verify", *sys.argv[1:]], commands=("verify",)))
    except Exception:
        try:
            os.write(
                1, b'{"tool":"query-passport","command":"verify","status":"failed","error":"PASSPORT_UNAVAILABLE"}\n'
            )
        except OSError:
            pass
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
