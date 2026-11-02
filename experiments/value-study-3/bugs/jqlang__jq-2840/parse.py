#!/usr/bin/env python3
# Log parser vendored from Multi-SWE-bench (github.com/multi-swe-bench/multi-swe-bench at 24f493f8,
# multi_swe_bench/harness/repos/c/jqlang/jq.py, Jq.parse_log).
# Multi-SWE-bench is licensed under the Apache License, Version 2.0:
#   Copyright (c) Multi-SWE-bench authors.
#   Licensed under the Apache License, Version 2.0 (the "License"); you may not use this file
#   except in compliance with the License. You may obtain a copy of the License at
#   http://www.apache.org/licenses/LICENSE-2.0
#   Unless required by applicable law or agreed to in writing, software distributed under the
#   License is distributed on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND.
# Usage: parse.py < test-log  ->  {"test name": "PASSED"|"FAILED"|"SKIPPED"} as JSON on stdout.
# A test both passed and failed in one log counts as failed.
import json
import re
import sys


def parse(log):
    passed, failed, skipped = set(), set(), set()
    re_pass = re.compile(r"^PASS: (\S+)$")
    re_fail = re.compile(r"^FAIL: (\S+)$")
    re_skip = re.compile(r"^SKIP: (\S+)$")
    for line in log.splitlines():
        line = line.strip()
        if not line:
            continue
        match = re_pass.match(line)
        if match:
            passed.add(match.group(1))
        match = re_fail.match(line)
        if match:
            failed.add(match.group(1))
        match = re_skip.match(line)
        if match:
            skipped.add(match.group(1))
    status = {}
    for name in skipped:
        status[name] = "SKIPPED"
    for name in passed:
        status[name] = "PASSED"
    for name in failed:
        status[name] = "FAILED"
    return status


if __name__ == "__main__":
    print(json.dumps(parse(sys.stdin.read()), indent=1, ensure_ascii=False, sort_keys=True))
