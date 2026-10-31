# sphinx-doc__sphinx-7889 (SWE-bench Verified). Sphinx at the base commit on Python 3.12, following
# the SWE-bench spec for sphinx 3.2: `pip install -e .[test]` with its version caps (applied as pip
# constraints instead of editing setup.py); unpinned packages resolve as of 2024-08-01, when the
# SWE-bench Verified environments were built.
FROM peeky-study/base:1
ARG BASE=ec9af606c6cfa515f946d74da9b51574f2f9b16f
RUN git clone -q https://github.com/sphinx-doc/sphinx.git /testbed && cd /testbed && \
    git checkout -q -B main $BASE && git remote remove origin && \
    git for-each-ref --format='%(refname)' | grep -vx refs/heads/main | xargs -r -n1 git update-ref -d && \
    git reflog expire --expire=now --all && git gc --prune=now -q && \
    test "$(git rev-parse HEAD)" = "$BASE" && \
    test "$(git rev-list --all | wc -l)" = "$(git rev-list HEAD | wc -l)" && \
    test -z "$(git fsck --unreachable --no-reflogs 2>/dev/null | head -1)"
RUN python3.12 -m venv /opt/venv
ENV VIRTUAL_ENV=/opt/venv PATH=/opt/venv/bin:$PATH
RUN printf '%s\n' 'Jinja2<3.0' 'markupsafe<=2.0.1' 'alabaster>=0.7,<0.7.12' \
        'sphinxcontrib-applehelp<=1.0.7' 'sphinxcontrib-devhelp<=1.0.5' 'sphinxcontrib-qthelp<=1.0.6' \
        'sphinxcontrib-htmlhelp<=2.0.4' 'sphinxcontrib-serializinghtml<=1.1.9' > /opt/constraints.txt && \
    UV_EXCLUDE_NEWER=2024-08-01T00:00:00Z uv pip install -q -c /opt/constraints.txt setuptools -e '.[test]'
RUN python -m pytest -rA tests/test_ext_autodoc_mock.py >/dev/null 2>&1 || true; \
    test -z "$(git status --porcelain)"
