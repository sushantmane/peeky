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


def parse_log_django(log):
    """
    Parser for test logs generated with Django tester framework

    Args:
        log (str): log content
    Returns:
        dict: test case to test status mapping
    """
    test_status_map = {}
    lines = log.split("\n")

    prev_test = None
    for line in lines:
        line = line.strip()

        # This isn't ideal but the test output spans multiple lines
        if "--version is equivalent to version" in line:
            test_status_map["--version is equivalent to version"] = (
                TestStatus.PASSED.value
            )

        # Log it in case of error
        if " ... " in line:
            prev_test = line.split(" ... ")[0]

        pass_suffixes = (" ... ok", " ... OK", " ...  OK")
        for suffix in pass_suffixes:
            if line.endswith(suffix):
                # TODO: Temporary, exclusive fix for django__django-7188
                # The proper fix should involve somehow getting the test results to
                # print on a separate line, rather than the same line
                if line.strip().startswith(
                    "Applying sites.0002_alter_domain_unique...test_no_migrations"
                ):
                    line = line.split("...", 1)[-1].strip()
                test = line.rsplit(suffix, 1)[0]
                test_status_map[test] = TestStatus.PASSED.value
                break
        if " ... skipped" in line:
            test = line.split(" ... skipped")[0]
            test_status_map[test] = TestStatus.SKIPPED.value
        if line.endswith(" ... FAIL"):
            test = line.split(" ... FAIL")[0]
            test_status_map[test] = TestStatus.FAILED.value
        if line.startswith("FAIL:"):
            test = line.split()[1].strip()
            test_status_map[test] = TestStatus.FAILED.value
        if line.endswith(" ... ERROR"):
            test = line.split(" ... ERROR")[0]
            test_status_map[test] = TestStatus.ERROR.value
        if line.startswith("ERROR:"):
            test = line.split()[1].strip()
            test_status_map[test] = TestStatus.ERROR.value

        if line.lstrip().startswith("ok") and prev_test is not None:
            # It means the test passed, but there's some additional output (including new lines)
            # between "..." and "ok" message
            test = prev_test
            test_status_map[test] = TestStatus.PASSED.value

    # TODO: This is very brittle, we should do better
    # There's a bug in the django logger, such that sometimes a test output near the end gets
    # interrupted by a particular long multiline print statement.
    # We have observed this in one of 3 forms:
    # - "{test_name} ... Testing against Django installed in {*} silenced.\nok"
    # - "{test_name} ... Internal Server Error: \/(.*)\/\nok"
    # - "{test_name} ... System check identified no issues (0 silenced).\nok"
    patterns = [
        r"^(.*?)\s\.\.\.\sTesting\ against\ Django\ installed\ in\ ((?s:.*?))\ silenced\)\.\nok$",
        r"^(.*?)\s\.\.\.\sInternal\ Server\ Error:\ \/(.*)\/\nok$",
        r"^(.*?)\s\.\.\.\sSystem check identified no issues \(0 silenced\)\nok$",
    ]
    for pattern in patterns:
        for match in re.finditer(pattern, log, re.MULTILINE):
            test_name = match.group(1)
            test_status_map[test_name] = TestStatus.PASSED.value
    return test_status_map


# Study adaptations (not in SWE-bench), for Django's runner on Python 3.11+:
# 1. unittest now names a test "test_x (pkg.module.Class.test_x)" where earlier Pythons, and the
#    SWE-bench test lists, have "test_x (pkg.module.Class)"; map the new form back.
# 2. A test with a docstring prints its name on one line and the docstring's first line, with the
#    result, on the next. SWE-bench lists such tests under either form, so report the result under
#    both the docstring line and the (mapped) test name.
_PY311_NAME = re.compile(r"^(\w+) \(([\w.]+)\.\1\)$")
_STATUS_SUFFIXES = (
    (" ... ok", "PASSED"), (" ... OK", "PASSED"), (" ...  OK", "PASSED"),
    (" ... FAIL", "FAILED"), (" ... ERROR", "ERROR"),
)


def _old_name(name):
    return _PY311_NAME.sub(r"\1 (\2)", name)


_parse_log_django_raw = parse_log_django


def parse_log_django(log):
    status_map = {_old_name(k): v for k, v in _parse_log_django_raw(log).items()}
    pending = None
    for line in log.split("\n"):
        line = line.strip()
        if pending is not None and " ... " in line:
            status = None
            for suffix, value in _STATUS_SUFFIXES:
                if line.endswith(suffix):
                    status = value
            if " ... skipped" in line:
                status = "SKIPPED"
            if status is not None:
                status_map.setdefault(pending, status)
        pending = _old_name(line) if (_PY311_NAME.match(line) and " ... " not in line) else None
    return status_map


if __name__ == "__main__":
    print(json.dumps(parse_log_django(sys.stdin.read()), indent=1, sort_keys=True))
