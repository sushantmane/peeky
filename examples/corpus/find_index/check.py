"""Independent checker: argv[1] is a candidate program path; never edited by agents."""

import runpy
import sys


def load(name):
    return runpy.run_path(sys.argv[1])[name]


f = load("find_index")
for items, target, expected in [(["a", "b", "c"], "c", 2), (["a"], "a", 0), (["a", "b"], "z", -1), ([], "a", -1)]:
    assert f(items, target) == expected, (items, target, f(items, target), expected)
print("PASS: find_index")
