#!/usr/bin/env python3
# Test-log parser for this bug: reads the test output on stdin and prints {"test": "PASSED"|"FAILED"}.
# Vendored from SWE-bench (swebench/harness/log_parsers/java.py, parse_log_ant).
# SWE-bench is MIT licensed: Copyright (c) 2023 Carlos E Jimenez, John Yang, Alexander Wettig,
# Shunyu Yao, Kexin Pei, Ofir Press, Karthik R Narasimhan.
import json
import re
import sys


def parse_log_ant(log: str) -> dict[str, str]:
    test_status_map = {}

    pattern = r"^\s*\[junit\]\s+\[(PASS|FAIL|ERR)\]\s+(.*)$"

    for line in log.split("\n"):
        match = re.match(pattern, line.strip())
        if match:
            status, test_name = match.groups()
            if status == "PASS":
                test_status_map[test_name] = "PASSED"
            elif status in ["FAIL", "ERR"]:
                test_status_map[test_name] = "FAILED"

    return test_status_map


if __name__ == "__main__":
    print(json.dumps(parse_log_ant(sys.stdin.read()), indent=1))
