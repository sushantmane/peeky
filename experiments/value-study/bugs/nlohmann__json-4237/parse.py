#!/usr/bin/env python3
# Log parser vendored from SWE-bench (swebench 4.1.0, swebench/harness/log_parsers/c.py).
# SWE-bench is MIT licensed:
#   MIT License
#   Copyright (c) 2023 Carlos E Jimenez, John Yang, Alexander Wettig, Shunyu Yao, Kexin Pei,
#   Ofir Press, Karthik R Narasimhan
#   Permission is hereby granted, free of charge, to any person obtaining a copy of this software
#   and associated documentation files (the "Software"), to deal in the Software without
#   restriction, including without limitation the rights to use, copy, modify, merge, publish,
#   distribute, sublicense, and/or sell copies of the Software, and to permit persons to whom the
#   Software is furnished to do so, subject to the following conditions: The above copyright
#   notice and this permission notice shall be included in all copies or substantial portions of
#   the Software. THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND.
# Usage: parse.py < test-log  ->  {"test name": "PASSED"|"FAILED"|"SKIPPED"} as JSON on stdout.
import json
import re
import sys

import xml.etree.ElementTree as ET


def parse(log):
    # parse_log_doctest (used for nlohmann/json); assumes the test binary runs with -s -r=xml.
    test_status_map = {}
    start_tag = "<doctest"
    end_tag = "</doctest>"
    start_index = log.find(start_tag)
    end_index = log.find(end_tag, start_index) + len(end_tag) if start_index != -1 else -1
    if start_index != -1 and end_index != -1:
        xml_string = log[start_index:end_index]
        try:
            root = ET.fromstring(xml_string)
        except ET.ParseError:
            # Deviation from upstream (which raises): an unparsable log means no test passed.
            return test_status_map
        for testcase in root.findall(".//TestCase"):
            testcase_name = testcase.get("name")
            for subcase in testcase.findall(".//SubCase"):
                subcase_name = subcase.get("name")
                name = f"{testcase_name} > {subcase_name}"
                expressions = subcase.findall(".//Expression")
                subcase_passed = all(expr.get("success") == "true" for expr in expressions)
                if subcase_passed:
                    test_status_map[name] = "PASSED"
                else:
                    test_status_map[name] = "FAILED"
    return test_status_map


if __name__ == "__main__":
    print(json.dumps(parse(sys.stdin.read()), indent=1, ensure_ascii=False))

