"""Independent checker: argv[1] is a candidate program path; never edited by agents."""

import runpy
import sys


def load(name):
    return runpy.run_path(sys.argv[1])[name]


f = load("latest")
for versions, expected in [(["9", "10"], "10"), (["1.9", "1.10"], "1.10"), (["10.1", "9.99"], "10.1"), (["2.0.1", "2.0.0"], "2.0.1")]:
    assert f(versions) == expected, (versions, f(versions), expected)
print("PASS: latest_version")
