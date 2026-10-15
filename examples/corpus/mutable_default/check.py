"""Independent checker: argv[1] is a candidate program path; never edited by agents."""

import runpy
import sys


def load(name):
    return runpy.run_path(sys.argv[1])[name]


f = load("add_tag")
assert f("x") == ["x"] and f("y") == ["y"] and f("z") == ["z"]
mine = ["k"]
assert f("v", mine) == ["k", "v"] and mine == ["k", "v"]
print("PASS: mutable_default")
