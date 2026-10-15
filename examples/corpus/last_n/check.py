"""Independent checker: argv[1] is a candidate program path; never edited by agents."""

import runpy
import sys


def load(name):
    return runpy.run_path(sys.argv[1])[name]


f = load("last_n")
for items, n, expected in [([1, 2, 3, 4], 2, [3, 4]), ([1, 2, 3], 3, [1, 2, 3]), ([1, 2], 0, []), ([5], 1, [5])]:
    assert f(items, n) == expected, (items, n, f(items, n), expected)
print("PASS: last_n")
