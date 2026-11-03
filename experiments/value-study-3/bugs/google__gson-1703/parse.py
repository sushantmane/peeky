#!/usr/bin/env python3
# Test-log parser for this bug: reads the test output on stdin and prints
# {"test class": "PASSED"|"FAILED"|"SKIPPED"} as JSON.
# Vendored from Multi-SWE-bench (multi_swe_bench/harness/repos/java/google/gson.py,
# Gson.parse_log): one status per test class, from surefire's "Running <class>" and the class's
# "Tests run: ..., Time elapsed: ..." line.
# Multi-SWE-bench is licensed under the Apache License, Version 2.0
# (http://www.apache.org/licenses/LICENSE-2.0). Changed: prints JSON instead of a TestResult; and,
# for this version's surefire (3.0.0-M7), the Maven log prefixes ("[INFO] ", "[ERROR] ",
# "[WARNING] ") are stripped first and the elapsed time's unit may be "s" as well as "sec".
import json
import re
import sys

PREFIX = re.compile(r"^\[(?:INFO|ERROR|WARNING)\] ", re.MULTILINE)


def parse_log(test_log):
    passed_tests = set()
    failed_tests = set()
    skipped_tests = set()

    test_log = PREFIX.sub("", test_log)

    re_pass_tests = [
        re.compile(
            r"Running\s+(.+?)\s*\n(?:(?!Tests run:).*\n)*Tests run:\s*(\d+),\s*Failures:\s*(\d+),\s*Errors:\s*(\d+),\s*Skipped:\s*(\d+),\s*Time elapsed:\s*[\d.]+\s*s(?:ec)?\b"
        )
    ]
    re_fail_tests = [
        re.compile(
            r"Running (.+?)\nTests run: (\d+), Failures: (\d+), Errors: (\d+), Skipped: (\d+), Time elapsed: [\d\.]+ s(?:ec)? +<<< FAILURE!"
        )
    ]

    for re_pass_test in re_pass_tests:
        tests = re_pass_test.findall(test_log, re.MULTILINE)
        for test in tests:
            test_name = test[0]
            tests_run = int(test[1])
            failures = int(test[2])
            errors = int(test[3])
            skipped = int(test[4])
            if tests_run > 0 and failures == 0 and errors == 0 and skipped != tests_run:
                passed_tests.add(test_name)
            elif failures > 0 or errors > 0:
                failed_tests.add(test_name)
            elif skipped == tests_run:
                skipped_tests.add(test_name)

    for re_fail_test in re_fail_tests:
        tests = re_fail_test.findall(test_log, re.MULTILINE)
        for test in tests:
            test_name = test[0]
            failed_tests.add(test_name)

    statuses = {}
    for name in skipped_tests:
        statuses[name] = "SKIPPED"
    for name in passed_tests:
        statuses[name] = "PASSED"
    for name in failed_tests:
        statuses[name] = "FAILED"
    return statuses


if __name__ == "__main__":
    print(json.dumps(parse_log(sys.stdin.read()), indent=1, sort_keys=True))
