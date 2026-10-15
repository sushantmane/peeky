"""Independent checker: argv[1] is a candidate program path; never edited by agents."""

import runpy
import sys


def load(name):
    return runpy.run_path(sys.argv[1])[name]


f = load("sum_to")
for n, expected in [(5, 15), (1, 1), (0, 0), (10, 55), (3, 6)]:
    assert f(n) == expected, (n, f(n), expected)
print("PASS: sum_to")
