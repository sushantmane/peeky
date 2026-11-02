# Round 3 candidates: py-a (Python, SWE-bench Verified)

| instance | control image + validation | Peeky image + gold/noop | reach (gold points: outcome, values read) | result | note |
|---|---|---|---|---|---|
| astropy__astropy-14598 | ok | ok (gold solved, noop not) | card.py:862 reached, 10 values (assertion reached) | survives | Py 3.11; build 0.9 min, grade 0.02 min |
| matplotlib__matplotlib-23476 | ok (1 p2p removed: arm64 image diff) | ok (gold solved, noop not) | figure.py:3027 reached, 2 values (assertion reached) | survives | Py 3.11; build 3.3 min, grade 0.1 min |
| sympy__sympy-17318 | ok | ok (gold solved, noop not) | sqrtdenest.py:159 reached, 2 values (assertion reached) | survives | Py 3.12; build 1.7 min, grade 0.06 min. Run correction 1: `--run bin/test` refused (INVALID_REQUEST "entry must be a .py file inside the root"); prepare copies bin/test to bin/reach_test.py, same args (record of run 1 in scratch/py-a/reach-old). |
| pylint-dev__pylint-8898 | ok | ok (gold solved, noop not) | argument.py:117 reached, 2 values (assertion reached) | survives | Py 3.12; build 0.2 min, grade 0.02 min |
| astropy__astropy-8707 | ok (pytest 7.4.0 → 7.1.3, see bug.yaml) | ok (gold solved, noop not) | card.py:557 reached, 2 values; header.py:395 no stop (not run by the test); assertion no stop (test raises before it at base) | survives | Py 3.11; build 3.1 min, grade 0.02 min |
| matplotlib__matplotlib-26208 | ok (2 p2p removed: arm64 image diffs) | ok (gold solved, noop not) | _base.py:4444 reached, 2 values; _base.py:4473 no stop (twiny, not run by test_twin_units[x]); assertion reached | survives | Py 3.11; build 3.4 min, grade 0.4 min |
| astropy__astropy-13977 | ok | ok (gold solved, noop not) | quantity.py:643 reached, 5 values; assertion no stop (test raises before it at base) | survives | Py 3.11; build 2.7 min, grade 0.02 min |
| matplotlib__matplotlib-24870 | ok (1 p2p removed: arm64 image diff) | ok (gold solved, noop not) | contour.py:1125 reached, 2 values; _tricontour.py:77 no stop (test fails before tricontour at base); assertion reached | survives | Py 3.11; build 4.2 min, grade 0.03 min |
| astropy__astropy-13236 | ok (Cython 0.29.22 → 0.29.30 to build on 3.11) | ok (gold solved, noop not) | table.py:1244 reached, 9 values (assertion reached) | survives | Py 3.11; build 0.9 min, grade 0.03 min |
| astropy__astropy-8872 | ok (pytest 7.1.3 as in 8707; SETUPTOOLS_USE_DISTUTILS=stdlib, see bug.yaml) | ok (gold solved, noop not) | quantity.py:299 reached, 7 values; quantity.py:380 reached, 8 values (assertion reached) | survives | Py 3.11; build 1.0 min (4.0 min uncached), grade 0.02 min |

All ten reach checks were run once more at the end (same specs and points) so their records carry
bug.yaml's language and category (the first runs preceded bug.yaml); outcomes and values are as above.

Counts: 10 examined; step 1 (control image + validation) 10 ok; step 2 (Peeky image, gold solved,
noop not) 10 ok; step 3 (reached) 10 reached. 10 survive, 0 dropped.
