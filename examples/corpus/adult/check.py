"""Independent checker: argv[1] is a candidate program path; never edited by agents."""

import runpy
import sys


def load(name):
    return runpy.run_path(sys.argv[1])[name]


f = load("is_adult")
for args, expected in [((18,), True), ((17,), False), ((30,), True), ((21, 21), True), ((20, 21), False), ((18, 21), False)]:
    assert f(*args) is expected, (args, f(*args), expected)
print("PASS: adult")
