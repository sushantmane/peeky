# pylint-dev__pylint-8898 (SWE-bench Verified). pylint at the base commit on Python 3.12, following
# the SWE-bench spec for pylint 3.0: the requirements_test.txt of the environment setup commit
# (a0ce6e424e, with requirements_test_min.txt; its `.[testutils,spelling]` line left out, as
# there), then astroid==3.0.0a6 and setuptools, then `pip install -e .`, which moves astroid into
# pyproject's range. Packages resolve as of 2024-08-01, when the SWE-bench Verified environments
# were built.
FROM peeky-study/base:1
ARG BASE=1f8c4d9eb185c16a2c1d881c054f015e1c2eb334
RUN git clone -q https://github.com/pylint-dev/pylint.git /testbed && cd /testbed && \
    git checkout -q -B main $BASE && git remote remove origin && \
    git for-each-ref --format='%(refname)' | grep -vx refs/heads/main | xargs -r -n1 git update-ref -d && \
    git reflog expire --expire=now --all && git gc --prune=now -q && \
    test "$(git rev-parse HEAD)" = "$BASE" && \
    test "$(git rev-list --all | wc -l)" = "$(git rev-list HEAD | wc -l)" && \
    test -z "$(git fsck --unreachable --no-reflogs 2>/dev/null | head -1)"
WORKDIR /testbed
RUN python3.12 -m venv /opt/venv
ENV VIRTUAL_ENV=/opt/venv PATH=/opt/venv/bin:$PATH
RUN export UV_EXCLUDE_NEWER=2024-08-01T00:00:00Z && \
    uv pip install -q 'coverage~=7.3' 'tbump~=6.10.0' 'contributors-txt>=1.0.0' 'pytest-cov~=4.1' \
        'pytest-profiling~=1.7' 'pytest-xdist~=3.3' six types-pkg_resources==0.1.3 'tox>=3' \
        astroid==3.0.0a9 'typing-extensions~=4.7' 'py~=1.11.0' 'pytest~=7.4' \
        'pytest-benchmark~=4.0' 'pytest-timeout~=2.1' 'towncrier~=23.6' requests \
        setuptools==41.6.0 && \
    uv pip install -q astroid==3.0.0a6 setuptools && \
    uv pip install -e . && \
    uv pip freeze | grep -E '^(astroid|pytest|setuptools|pylint)( @|==)'
RUN pytest -rA tests/config/test_config.py >/dev/null 2>&1 || true; \
    test -z "$(git status --porcelain)"
