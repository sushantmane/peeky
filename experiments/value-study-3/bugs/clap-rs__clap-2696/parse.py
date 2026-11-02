#!/usr/bin/env python3
# Test-log parser for this bug: reads the test output on stdin, prints {"test": "PASSED"|"FAILED"|"SKIPPED"}.
# Vendored from Multi-SWE-bench (multi_swe_bench/harness/repos/rust/<org>/<repo>.py, parse_log; the
# same function for nushell, clap and ripgrep). A test the log shows both passing and failing (in
# two test binaries) is FAILED here; Multi-SWE-bench rejects such a log.
# Multi-SWE-bench: Copyright (c) 2024 Bytedance Ltd. and/or its affiliates.
#   Licensed under the Apache License, Version 2.0 (the "License"); you may not use this file
#   except in compliance with the License. You may obtain a copy of the License at
#       http://www.apache.org/licenses/LICENSE-2.0
#   Unless required by applicable law or agreed to in writing, software distributed under the
#   License is distributed on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND,
#   either express or implied. See the License for the specific language governing permissions
#   and limitations under the License.
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


if __name__ == "__main__":
    passed, failed, skipped = parse_log(sys.stdin.read())
    statuses = {}
    for test in sorted(skipped):
        statuses[test] = "SKIPPED"
    for test in sorted(passed):
        statuses[test] = "PASSED"
    for test in sorted(failed):
        statuses[test] = "FAILED"
    print(json.dumps(statuses, indent=1))
