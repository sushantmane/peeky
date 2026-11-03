#!/usr/bin/env python3
# Log parser vendored from Multi-SWE-bench (github.com/multi-swe-bench/multi-swe-bench at 24f493f8,
# multi_swe_bench/harness/repos/c/facebook/zstd.py, Zstd.parse_log), with the branch for its PRs
# 1831 to 2703, which this PR (2130) takes: "all tests" passes when the log holds make's line
# for leaving doc/educational_decoder once (`make test` reached its last step), else fails. The
# repository is at /testbed here, not /home/zstd.
# Multi-SWE-bench is licensed under the Apache License, Version 2.0:
#   Copyright (c) Multi-SWE-bench authors.
#   Licensed under the Apache License, Version 2.0 (the "License"); you may not use this file
#   except in compliance with the License. You may obtain a copy of the License at
#   http://www.apache.org/licenses/LICENSE-2.0
#   Unless required by applicable law or agreed to in writing, software distributed under the
#   License is distributed on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND.
# Usage: parse.py < test-log  ->  {"all tests": "PASSED"|"FAILED"} as JSON on stdout.
import json
import re
import sys


def parse(log):
    keyword = "make[1]: Leaving directory '/testbed/doc/educational_decoder'"
    count = len(re.findall(re.escape(keyword), log))
    return {"all tests": "PASSED" if count == 1 else "FAILED"}


if __name__ == "__main__":
    print(json.dumps(parse(sys.stdin.read()), indent=1, ensure_ascii=False, sort_keys=True))
