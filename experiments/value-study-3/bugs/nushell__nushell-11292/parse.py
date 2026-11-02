#!/usr/bin/env python3
# Test-log parser for this bug: reads the test output on stdin, prints {"test": "PASSED"|"FAILED"|
# "SKIPPED"} as JSON.
# Vendored from Multi-SWE-bench (https://github.com/multi-swe-bench/multi-swe-bench),
# multi_swe_bench/harness/repos/rust/nushell/nushell.py, Nushell.parse_log (the same parser serves
# its Rust repos). Multi-SWE-bench is licensed under the Apache License, Version 2.0
# (http://www.apache.org/licenses/LICENSE-2.0); this file is a modified copy: it reads stdin and
# prints a status per test instead of building a TestResult. A name that both passed and failed
# (the same test name in two test binaries) is reported FAILED.
import json
import re
import sys


def parse_log(test_log):
    passed_tests = set()
    failed_tests = set()
    skipped_tests = set()

    re_pass_tests = [re.compile(r"test (\S+) ... ok")]
    re_fail_tests = [re.compile(r"test (\S+) ... FAILED")]
    re_skip_tests = [re.compile(r"test (\S+) ... ignored")]

    for line in test_log.splitlines():
        line = line.strip()

        for re_pass in re_pass_tests:
            match = re_pass.match(line)
            if match:
                passed_tests.add(match.group(1))

        for re_fail in re_fail_tests:
            match = re_fail.match(line)
            if match:
                failed_tests.add(match.group(1))

        for re_skip in re_skip_tests:
            match = re_skip.match(line)
            if match:
                skipped_tests.add(match.group(1))

    return passed_tests, failed_tests, skipped_tests


def statuses(test_log):
    passed, failed, skipped = parse_log(test_log)
    result = {}
    for name in skipped:
        result[name] = "SKIPPED"
    for name in passed:
        result[name] = "PASSED"
    for name in failed:
        result[name] = "FAILED"
    return result


if __name__ == "__main__":
    print(json.dumps(statuses(sys.stdin.read()), indent=1, sort_keys=True))
