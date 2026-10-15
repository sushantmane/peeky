"""Independent checker: argv[1] is a candidate program path; never edited by agents."""

import runpy
import sys


def load(name):
    return runpy.run_path(sys.argv[1])[name]


f = load("running_max")
for values, expected in [
    ([-3, -1, -2], [-3, -1, -1]),
    ([1, 3, 2], [1, 3, 3]),
    ([-10, -20], [-10, -10]),
    ([], []),
]:
    assert f(values) == expected, (values, f(values), expected)
print("PASS: running_max")
