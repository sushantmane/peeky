#!/usr/bin/env python3
"""Reads a test log on stdin and prints {"test name": "PASSED"|"FAILED"|...} as JSON.

Vendored from SWE-bench (swebench/harness/log_parsers/python.py, swebench 3.0.17):

    MIT License

    Copyright (c) 2023 Carlos E Jimenez, John Yang, Alexander Wettig, Shunyu Yao, Kexin Pei,
    Ofir Press, Karthik R Narasimhan

    Permission is hereby granted, free of charge, to any person obtaining a copy of this software
    and associated documentation files (the "Software"), to deal in the Software without
    restriction, including without limitation the rights to use, copy, modify, merge, publish,
    distribute, sublicense, and/or sell copies of the Software, and to permit persons to whom the
    Software is furnished to do so, subject to the following conditions:

    The above copyright notice and this permission notice shall be included in all copies or
    substantial portions of the Software.

    THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING
    BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
    NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM,
    DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
    OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
"""
import json
import re
import sys
from enum import Enum


class TestStatus(Enum):
    FAILED = "FAILED"
    PASSED = "PASSED"
    SKIPPED = "SKIPPED"
    ERROR = "ERROR"
    XFAIL = "XFAIL"


def parse_log_pytest_v2(log):
    """
    Parser for test logs generated with PyTest framework (Later Version)

    Args:
        log (str): log content
    Returns:
        dict: test case to test status mapping
    """
    test_status_map = {}
    escapes = "".join([chr(char) for char in range(1, 32)])
    for line in log.split("\n"):
        line = re.sub(r"\[(\d+)m", "", line)
        translator = str.maketrans("", "", escapes)
        line = line.translate(translator)
        if any([line.startswith(x.value) for x in TestStatus]):
            if line.startswith(TestStatus.FAILED.value):
                line = line.replace(" - ", " ")
            test_case = line.split()
            if len(test_case) >= 2:
                test_status_map[test_case[1]] = test_case[0]
        # Support older pytest versions by checking if the line ends with the test status
        elif any([line.endswith(x.value) for x in TestStatus]):
            test_case = line.split()
            if len(test_case) >= 2:
                test_status_map[test_case[0]] = test_case[1]
    return test_status_map


parse_log_astropy = parse_log_pytest_v2


if __name__ == "__main__":
    print(json.dumps(parse_log_astropy(sys.stdin.read()), indent=1, sort_keys=True))
