"""Independent checker: argv[1] is a candidate program path; never edited by agents."""

import runpy
import sys


def load(name):
    return runpy.run_path(sys.argv[1])[name]


f = load("average")
for values, expected in [
    ([1, 2], 1.5),
    ([1, 2, 3, 4], 2.5),
    ([5], 5),
    ([2, 4], 3),
    ([1, 1, 2], 4 / 3),
]:
    assert abs(f(values) - expected) < 1e-9, (values, f(values), expected)
print("PASS: average")
