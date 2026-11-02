#!/usr/bin/env python3
# Log parser vendored from Multi-SWE-bench (github.com/multi-swe-bench/multi-swe-bench at
# 24f493f8, multi_swe_bench/harness/repos/cpp/catchorg/catch2.py, Catch2.parse_log): ctest's
# summary lines, and CMake's "Performing Test" checks, lower-cased. Multi-SWE-bench is licensed
# under the Apache License, Version 2.0 (http://www.apache.org/licenses/LICENSE-2.0); this file is
# a modified copy (stdlib only, prints JSON). Distributed on an "AS IS" BASIS, WITHOUT WARRANTIES
# OR CONDITIONS OF ANY KIND.
# Usage: parse.py < test-log  ->  {"test name": "PASSED"|"FAILED"|"SKIPPED"} as JSON on stdout.
import json
import re
import sys


def parse(test_log):
    passed_tests = set()
    failed_tests = set()
    skipped_tests = set()

    re_passes = [
        re.compile(r"^-- Performing Test (.+) - Success$", re.IGNORECASE),
        re.compile(r"^\d+/\d+ Test\s+#\d+: (.+) \.+\s+ Passed\s+.+$", re.IGNORECASE),
    ]
    re_fails = [
        re.compile(r"^-- Performing Test (.+) - Failed$", re.IGNORECASE),
        re.compile(r"^\d+/\d+ Test\s+#\d+: (.+) \.+\*\*\*Failed\s+.+$", re.IGNORECASE),
    ]
    re_skips = [
        re.compile(r"^-- Performing Test (.+) - skipped$", re.IGNORECASE),
    ]

    for line in test_log.splitlines():
        line = line.strip().lower()
        if not line:
            continue
        for re_pass in re_passes:
            pass_match = re_pass.match(line)
            if pass_match:
                passed_tests.add(pass_match.group(1))
        for re_fail in re_fails:
            fail_match = re_fail.match(line)
            if fail_match:
                failed_tests.add(fail_match.group(1))
        for re_skip in re_skips:
            skip_match = re_skip.match(line)
            if skip_match:
                skipped_tests.add(skip_match.group(1))

    statuses = {test: "SKIPPED" for test in skipped_tests}
    statuses.update({test: "PASSED" for test in passed_tests})
    statuses.update({test: "FAILED" for test in failed_tests})
    return statuses


if __name__ == "__main__":
    print(json.dumps(parse(sys.stdin.read()), indent=1, ensure_ascii=False))
