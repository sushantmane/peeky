#!/usr/bin/env python3
# Test-log parser for this bug: reads the test output on stdin and prints {"test": "PASSED"|"FAILED"}.
# Vendored from SWE-bench (swebench/harness/log_parsers/java.py, parse_log_maven). It needs each
# test run on its own with the command traced (`set -x`), and reads BUILD SUCCESS/FAILURE.
# SWE-bench is MIT licensed: Copyright (c) 2023 Carlos E Jimenez, John Yang, Alexander Wettig,
# Shunyu Yao, Kexin Pei, Ofir Press, Karthik R Narasimhan.
import json
import re
import sys


def parse_log_maven(log: str) -> dict[str, str]:
    test_status_map = {}
    current_test_name = "---NO TEST NAME FOUND YET---"

    # Get the test name from the command used to execute the test.
    # Assumes we run evaluation with set -x
    test_name_pattern = r"^.*-Dtest=(\S+).*$"
    result_pattern = r"^.*BUILD (SUCCESS|FAILURE)$"

    for line in log.split("\n"):
        test_name_match = re.match(test_name_pattern, line.strip())
        if test_name_match:
            current_test_name = test_name_match.groups()[0]

        result_match = re.match(result_pattern, line.strip())
        if result_match:
            status = result_match.groups()[0]
            if status == "SUCCESS":
                test_status_map[current_test_name] = "PASSED"
            elif status == "FAILURE":
                test_status_map[current_test_name] = "FAILED"

    return test_status_map


if __name__ == "__main__":
    print(json.dumps(parse_log_maven(sys.stdin.read()), indent=1))
