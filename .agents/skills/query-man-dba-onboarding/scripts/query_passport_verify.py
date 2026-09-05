#!/usr/bin/env python3
"""DBA Execute entrypoint; authorization remains with the skill and executor."""

import os
import runpy
import sys
from pathlib import Path


def main() -> int:
    command = "verify"
    try:
        shared = Path(__file__).parents[2] / "query-man-admin/scripts/query_passport.py"
        consumer = runpy.run_path(str(shared))
        commands = ("verify", "prepare", "issue", "apply", "deliver", "rotate", "rollback", "status")
        arguments = sys.argv[1:]
        # Preserve the original verify-only invocation when arguments start with flags.
        if not arguments or arguments[0].startswith("-"):
            arguments = ["verify", *arguments]
        if arguments[0] in commands:
            command = arguments[0]
        return int(consumer["run"](arguments, commands=commands))
    except Exception:
        try:
            import json

            os.write(
                1,
                (
                    json.dumps(
                        {
                            "tool": "query-passport",
                            "command": command,
                            "status": "failed",
                            "error": "PASSPORT_UNAVAILABLE",
                        },
                        separators=(",", ":"),
                    )
                    + "\n"
                ).encode(),
            )
        except OSError:
            pass
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
