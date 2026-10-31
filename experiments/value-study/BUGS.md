# The study's bugs

Published before any agent run (issue #441). The selection procedure is in [README.md](README.md),
and every candidate examined, with the reason for each skip, is in `candidates/<language>-log.md`.
Each bug's directory under `bugs/` holds its problem statement, image, hidden tests, upstream fix,
grading and validation.

Every bug was validated in fresh containers with no network, in both arms:

- the hidden tests fail at the base commit;
- they pass with the upstream fix, on two runs;
- an unchanged checkout is not solved.

| # | Bug | Language | Category | Also applies | Upstream fix |
|---|---|---|---|---|---|
| 1 | `astropy__astropy-13398` | python | B | [B, D] | https://github.com/astropy/astropy/pull/13398 |
| 2 | `django__django-11087` | python | D | [D, B] | https://github.com/django/django/pull/11087 |
| 3 | `mwaskom__seaborn-3069` | python | B | [B] | https://github.com/mwaskom/seaborn/pull/3069 |
| 4 | `mwaskom__seaborn-3187` | python | B | [B] | https://github.com/mwaskom/seaborn/pull/3187 |
| 5 | `pallets__flask-5014` | python | E | [E] | https://github.com/pallets/flask/pull/5014 |
| 6 | `psf__requests-6028` | python | B | [B, D] | https://github.com/psf/requests/pull/6028 |
| 7 | `pydata__xarray-6599` | python | B | [B] | https://github.com/pydata/xarray/pull/6599 |
| 8 | `sphinx-doc__sphinx-10614` | python | D | [D] | https://github.com/sphinx-doc/sphinx/pull/10614 |
| 9 | `sphinx-doc__sphinx-7889` | python | E | [E] | https://github.com/sphinx-doc/sphinx/pull/7889 |
| 10 | `apache__druid-15402` | java | D | [D, A] | https://github.com/apache/druid/pull/15402 |
| 11 | `apache__lucene-12022` | java | D | [D, A] | https://github.com/apache/lucene/pull/12022 |
| 12 | `apache__lucene-13704` | java | D | [D, A] | https://github.com/apache/lucene/pull/13704 |
| 13 | `projectlombok__lombok-3009` | java | E | [E] | https://github.com/projectlombok/lombok/pull/3009 |
| 14 | `projectlombok__lombok-3052` | java | B | [B] | https://github.com/projectlombok/lombok/pull/3052 |
| 15 | `projectlombok__lombok-3326` | java | B | [B, D] | https://github.com/projectlombok/lombok/pull/3326 |
| 16 | `reactivex__rxjava-7597` | java | D | [D] | https://github.com/reactivex/rxjava/pull/7597 |
| 17 | `jqlang__jq-2650` | c | E | [E] | https://github.com/jqlang/jq/pull/2650 |
| 18 | `micropython__micropython-13569` | c | C | [C, D] | https://github.com/micropython/micropython/pull/13569 |
| 19 | `valkey-io__valkey-1842` | c | D | [D, C] | https://github.com/valkey-io/valkey/pull/1842 |
| 20 | `valkey-io__valkey-790` | c | D | [D, C] | https://github.com/valkey-io/valkey/pull/790 |
| 21 | `fmtlib__fmt-3158` | cpp | C | [C, B] | https://github.com/fmtlib/fmt/pull/3158 |
| 22 | `fmtlib__fmt-3272` | cpp | C | [C, D] | https://github.com/fmtlib/fmt/pull/3272 |
| 23 | `nlohmann__json-4237` | cpp | E | [E] | https://github.com/nlohmann/json/pull/4237 |
| 24 | `burntsushi__ripgrep-2209` | rust | D | [D, C] | https://github.com/burntsushi/ripgrep/pull/2209 |
| 25 | `sharkdp__bat-3108` | rust | B | [B, D] | https://github.com/sharkdp/bat/pull/3108 |
| 26 | `tokio-rs__axum-1934` | rust | D | [D, C] | https://github.com/tokio-rs/axum/pull/1934 |
| 27 | `tokio-rs__tokio-4867` | rust | C | [C, D] | https://github.com/tokio-rs/tokio/pull/4867 |
| 28 | `tokio-rs__tokio-6752` | rust | D | [D, C] | https://github.com/tokio-rs/tokio/pull/6752 |
| 29 | `uutils__coreutils-6690` | rust | E | [E, B] | https://github.com/uutils/coreutils/pull/6690 |


**By language:** Python 9, Java 7, C 4, C++ 3, Rust 6.

**By primary category:** B 8, C 4, D 11, E (control) 6.

A, "slow build or long test setup", is a primary category for none of them. No project took 3
minutes to build and run its relevant tests in the image. The three Lucene and Druid bugs need a
large multi-module build, so A also applies to them.
