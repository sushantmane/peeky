#!/usr/bin/env python3
# Log parser vendored from Multi-SWE-bench (github.com/multi-swe-bench/multi-swe-bench at 24f493f8,
# multi_swe_bench/harness/repos/c/ponylang/ponyc.py, Ponyc.parse_log): Google Test's OK/FAILED
# lines (plain or coloured) and PonyTest's "---- Passed:"/"**** FAILED:" lines.
# Multi-SWE-bench is licensed under the Apache License, Version 2.0:
#   Copyright (c) Multi-SWE-bench authors.
#   Licensed under the Apache License, Version 2.0 (the "License"); you may not use this file
#   except in compliance with the License. You may obtain a copy of the License at
#   http://www.apache.org/licenses/LICENSE-2.0
#   Unless required by applicable law or agreed to in writing, software distributed under the
#   License is distributed on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND.
# Usage: parse.py < test-log  ->  {"test name": "PASSED"|"FAILED"} as JSON on stdout.
# A test both passed and failed in one log counts as failed.
import json
import re
import sys

RE_PASSES = [
    re.compile(r"^\[       OK \] (\S+?)( \(.+\))?$"),
    re.compile(r"^\x1b\[0;32m\[       OK \] \x1b\[0m(\S+?)( \(.+\))?$"),
    re.compile(r"^\x1b\[92m---- Passed: (\S+?)\x1b\[0m$"),
]
RE_FAILS = [
    re.compile(r"^\[  FAILED  \] (\S+?)( \(.+\))?$"),
    re.compile(r"^\x1b\[0;31m\[  FAILED  \] \x1b\[0m(\S+?)( \(.+\))?$"),
    re.compile(r"^\x1b\[91m\*\*\*\* FAILED: (\S+?)\x1b\[0m$"),
]


def parse(log):
    passed, failed = set(), set()
    for line in log.splitlines():
        line = line.strip()
        if not line:
            continue
        for pattern in RE_PASSES:
            match = pattern.match(line)
            if match:
                passed.add(match.group(1))
        for pattern in RE_FAILS:
            match = pattern.match(line)
            if match:
                failed.add(match.group(1))
    status = {name: "PASSED" for name in passed}
    status.update({name: "FAILED" for name in failed})
    return status


if __name__ == "__main__":
    print(json.dumps(parse(sys.stdin.read()), indent=1, ensure_ascii=False, sort_keys=True))
