#!/usr/bin/env python3
# Test-log parser for this bug: reads the test output on stdin and prints
# {"test or Gradle task": "PASSED"|"FAILED"|"SKIPPED"} as JSON.
# Vendored from Multi-SWE-bench (multi_swe_bench/harness/repos/java/elastic/logstash.py,
# Logstash.parse_log): Gradle's "> Task :name [UP-TO-DATE|FAILED|SKIPPED|NO-SOURCE]" lines, and the
# build's testLogging lines "Class > method PASSED|FAILED|SKIPPED".
# Multi-SWE-bench is licensed under the Apache License, Version 2.0
# (http://www.apache.org/licenses/LICENSE-2.0). Changed: prints JSON instead of a TestResult.
import json
import re
import sys


def parse_log(test_log):
    passed_tests = set()
    failed_tests = set()
    skipped_tests = set()

    passed_res = [
        re.compile(r"^> Task :(\S+)$"),
        re.compile(r"^> Task :(\S+) UP-TO-DATE$"),
        re.compile(r"^(.+ > .+) PASSED$"),
    ]

    failed_res = [
        re.compile(r"^> Task :(\S+) FAILED$"),
        re.compile(r"^(.+ > .+) FAILED$"),
    ]

    skipped_res = [
        re.compile(r"^> Task :(\S+) SKIPPED$"),
        re.compile(r"^> Task :(\S+) NO-SOURCE$"),
        re.compile(r"^(.+ > .+) SKIPPED$"),
    ]

    for line in test_log.splitlines():
        for passed_re in passed_res:
            m = passed_re.match(line)
            if m and m.group(1) not in failed_tests:
                passed_tests.add(m.group(1))

        for failed_re in failed_res:
            m = failed_re.match(line)
            if m:
                failed_tests.add(m.group(1))
                if m.group(1) in passed_tests:
                    passed_tests.remove(m.group(1))

        for skipped_re in skipped_res:
            m = skipped_re.match(line)
            if m:
                skipped_tests.add(m.group(1))

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
