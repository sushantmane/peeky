"""Independent checker: argv[1] is a candidate program path; never edited by agents."""

import runpy
import sys


def load(name):
    return runpy.run_path(sys.argv[1])[name]


f = load("dedupe")
assert f(["A@x.com", "a@x.com"]) == ["a@x.com"]
assert f(["Bob@Y.org", " bob@y.org ", "c@z.io"]) == ["bob@y.org", "c@z.io"]
assert f([]) == []
print("PASS: dedupe")
