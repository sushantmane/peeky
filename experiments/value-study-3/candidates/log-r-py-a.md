# Round 3 refill: r-py-a (Python, SWE-bench Verified)

| instance | refill # | control image + validation | Peeky image + gold/noop | reach (gold points: outcome, values read) | result | note |
|---|---|---|---|---|---|---|
| matplotlib__matplotlib-23299 | 1 | ok | ok (gold solved, noop not) | __init__.py:1090 reached, 2 values (assertion reached) | survives | Py 3.11; build 10.6 min (9.3 of it the clone), grade 0.02 min. Reach run again after bug.yaml (for its language and category), same points and outcomes. |
| astropy__astropy-7606 | 7 | fail | — | — | dropped at step 1: Python project doesn't run on CPython 3.11 or 3.12 | astropy 3.1.dev (SWE-bench spec: Python 3.6). `import astropy` fails on 3.10+: the bundled configobj uses `collections.Mapping` (AttributeError), so setup.py's import of astropy stopped the image build at `pip install -e .`; no image produced |
| scikit-learn__scikit-learn-26194 | 13 | ok (numpy 1.23.2, scipy 1.9.3 for 3.11; pip 24.2, setuptools 72.1.0 pinned, see bug.yaml) | ok (gold solved, noop not) | _ranking.py:1086 reached, 9 values (assertion reached) | survives | Py 3.11; build 2.0 min, grade 0.02 min |
| matplotlib__matplotlib-21568 | 19 | ok (7 p2p removed: image tests fail on Py 3.11, savefig's keyword check; numpy 1.24.4, see bug.yaml) | ok (gold solved, noop not) | dates.py:599 reached, 3 values (assertion reached) | survives | Py 3.11; build 4.1 min (2.9 of it the clone), grade 0.03 min. First validation ran with all 73 p2p (gold-1 unsolved for the 7 image tests); redone after removing them |
| astropy__astropy-13033 | 25 | ok (Cython 0.29.30 as in 13236) | ok (gold solved, noop not) | core.py:79 reached, 3 values (assertion reached) | survives | Py 3.11; build 1.7 min, grade 0.02 min. First hunk only adds a nested helper (a declaration), so the one gold point is the second hunk's raise |
| sphinx-doc__sphinx-9461 | 31 | fail | — | — | dropped at step 1: Python project doesn't run on CPython 3.11 or 3.12 | As sphinx-9229: sphinx/util/typing.py imports `types.Union` on Python > 3.10 (a name only 3.10's betas had), so `import sphinx.util` fails on 3.11 and 3.12; base grading on 3.12 ended at pytest's plugin import (every test missing). Build 2.0 min; image removed |
| sphinx-doc__sphinx-7590 | 39 | ok | ok (gold solved, noop not) | c.py:2079 no stop (C domain; no failing test runs it); cfamily.py:72 reached, 1 value (assertion reached) | survives | Py 3.12; build 2.0 min (1.4 of it the clone), grade 0.03 min |

Counts: 7 examined; step 1 (control image + validation) 5 ok, 2 dropped (Python project doesn't run on
CPython 3.11 or 3.12: astropy-7606, sphinx-9461); step 2 (Peeky image, gold solved, noop not) 5 ok;
step 3 (reached) 5 reached. 5 survive, 2 dropped.
