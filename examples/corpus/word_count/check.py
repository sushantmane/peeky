"""Independent checker: argv[1] is a candidate program path; never edited by agents."""

import runpy
import sys


def load(name):
    return runpy.run_path(sys.argv[1])[name]


f = load("count_words")
assert f("a b a") == {"a": 2, "b": 1}
assert f("x") == {"x": 1}
assert f("") == {}
assert f("q q q r") == {"q": 3, "r": 1}
print("PASS: word_count")
