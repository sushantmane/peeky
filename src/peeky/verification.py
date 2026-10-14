"""Trusted checker completion wrapper; this is not a security sandbox.

A checker accepts a candidate path in argv[1] and must return normally after
all assertions. Any SystemExit (including zero), exception or missing completion
is failure. Candidate code shares the checker process and must be trusted.
"""

import json
import runpy
import sys
from pathlib import Path


def main():
    check, candidate, completion = sys.argv[1:]
    sys.argv = [check, candidate]
    sys.path.insert(0, str(Path(check).resolve().parent))
    result = {"completed": False}
    try:
        runpy.run_path(check, run_name="__main__")
    except BaseException as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
    else:
        result["completed"] = True
    Path(completion).write_text(json.dumps(result))
    return 0 if result["completed"] else 1


if __name__ == "__main__":
    sys.exit(main())
