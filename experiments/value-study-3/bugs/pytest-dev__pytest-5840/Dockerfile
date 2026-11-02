# pytest-dev__pytest-5840 (SWE-bench Verified). pytest at the base commit on Python 3.11 (uv), following
# the SWE-bench spec for pytest 5.1: its pinned packages, then `pip install -e .`. Not 3.12: this
# pytest's assertion rewriting uses ast.Str and ast.Num, deprecated in 3.12, and its own config
# turns warnings into errors. The checkout has no tags, so setuptools_scm is given the version
# (5.1.3.dev0) that a tagged checkout would start with; tox.ini asks for pytest >= 2.0.
FROM peeky-study/base:1
ARG BASE=73c5b7f4b11a81e971f7d1bb18072e06a87060f4
RUN git clone -q https://github.com/pytest-dev/pytest.git /testbed && cd /testbed && \
    git checkout -q -B main $BASE && git remote remove origin && \
    git for-each-ref --format='%(refname)' | grep -vx refs/heads/main | xargs -r -n1 git update-ref -d && \
    git reflog expire --expire=now --all && git gc --prune=now -q && \
    test "$(git rev-parse HEAD)" = "$BASE" && \
    test "$(git rev-list --all | wc -l)" = "$(git rev-list HEAD | wc -l)" && \
    test -z "$(git fsck --unreachable --no-reflogs 2>/dev/null | head -1)"
RUN uv venv -q --seed -p 3.11 /opt/venv
ENV VIRTUAL_ENV=/opt/venv PATH=/opt/venv/bin:$PATH
RUN export UV_EXCLUDE_NEWER=2024-08-01T00:00:00Z SETUPTOOLS_SCM_PRETEND_VERSION=5.1.3.dev0 && \
    uv pip install -q atomicwrites==1.4.1 attrs==23.1.0 more-itertools==10.1.0 packaging==23.1 \
        pluggy==0.13.1 py==1.11.0 wcwidth==0.2.6 && \
    uv pip install -q setuptools -e . && \
    uv pip freeze | grep -iE '^(pluggy|attrs|py)==' && python -m pytest --version
RUN python -m pytest -rA testing/test_conftest.py >/dev/null 2>&1 || true; \
    rm -rf /tmp/* && test -z "$(git status --porcelain)"
