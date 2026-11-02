#!/usr/bin/env python3
# Log parser vendored from Multi-SWE-bench (github.com/multi-swe-bench/multi-swe-bench at
# 24f493f8, multi_swe_bench/harness/repos/cpp/fmtlib/fmt.py, Fmt.parse_log): ctest's summary
# lines. Multi-SWE-bench is licensed under the Apache License, Version 2.0
# (http://www.apache.org/licenses/LICENSE-2.0); this file is a modified copy (stdlib only, prints
# JSON). Distributed on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND.
# Usage: parse.py < test-log  ->  {"test name": "PASSED"|"FAILED"} as JSON on stdout. A test that
# ctest reports otherwise (an abort, a crash, "Not Run") is left out, so it isn't passing.
import json
import re
import sys


def parse(test_log):
    passed_tests = set()
    failed_tests = set()

    re_pass_tests = [re.compile(r"^\d+/\d+\s*Test\s*#\d+:\s*(.*?)\s*\.+\s*Passed")]
    re_fail_tests = [re.compile(r"^\d+/\d+\s*Test\s*#\d+:\s*(.*?)\s*\.+\s*\*+Failed$")]

    for line in test_log.splitlines():
        line = line.strip()
        if not line:
            continue
        for re_pass_test in re_pass_tests:
            pass_match = re_pass_test.match(line)
            if pass_match:
                passed_tests.add(pass_match.group(1))
        for re_fail_test in re_fail_tests:
            fail_match = re_fail_test.match(line)
            if fail_match:
                failed_tests.add(fail_match.group(1))

    statuses = {test: "PASSED" for test in passed_tests}
    statuses.update({test: "FAILED" for test in failed_tests})
    return statuses


if __name__ == "__main__":
    print(json.dumps(parse(sys.stdin.read()), indent=1, ensure_ascii=False))
