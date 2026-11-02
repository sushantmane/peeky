#!/usr/bin/env python3
# Test-log parser for this bug: reads the test output on stdin and prints
# {"test class": "PASSED"|"FAILED"|"SKIPPED"} as JSON.
# Vendored from Multi-SWE-bench (multi_swe_bench/harness/repos/java/fasterxml/jackson_databind.py,
# JacksonDatabind.parse_log): one status per test class, from surefire's per-class
# "Tests run: ..., Time elapsed: ... in <class>" lines.
# Multi-SWE-bench is licensed under the Apache License, Version 2.0
# (http://www.apache.org/licenses/LICENSE-2.0). Changed: prints JSON instead of a TestResult.
import json
import re
import sys


def parse_log(test_log):
    passed_tests = set()
    failed_tests = set()
    skipped_tests = set()

    def remove_ansi_escape_sequences(text):
        ansi_escape_pattern = re.compile(r"\x1B\[[0-?9;]*[mK]")
        return ansi_escape_pattern.sub("", text)

    test_log = remove_ansi_escape_sequences(test_log)

    pattern = re.compile(
        r"Tests run: (\d+), Failures: (\d+), Errors: (\d+), Skipped: (\d+), Time elapsed: [\d.]+ .+? in (.+)"
    )

    for line in test_log.splitlines():
        match = pattern.search(line)
        if match:
            tests_run = int(match.group(1))
            failures = int(match.group(2))
            errors = int(match.group(3))
            skipped = int(match.group(4))
            test_name = match.group(5)

            if tests_run > 0 and failures == 0 and errors == 0 and skipped != tests_run:
                passed_tests.add(test_name)
            elif failures > 0 or errors > 0:
                failed_tests.add(test_name)
            elif skipped == tests_run:
                skipped_tests.add(test_name)

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
