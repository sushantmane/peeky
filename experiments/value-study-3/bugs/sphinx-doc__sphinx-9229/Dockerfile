# sphinx-doc__sphinx-9229 (SWE-bench Verified). Sphinx at the base commit on Python 3.12, following
# the SWE-bench spec for sphinx 4.1: `pip install -e .[test]` with its version caps (applied as pip
# constraints instead of editing setup.py); unpinned packages resolve as of 2024-08-01, when the
# SWE-bench Verified environments were built. Tests run with tox.ini's settings, without tox.
FROM peeky-study/base:1
ARG BASE=876fa81e0a038cda466925b85ccf6c5452e0f685
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
    UV_EXCLUDE_NEWER=2024-08-01T00:00:00Z uv pip install -q -c /opt/constraints.txt setuptools -e '.[test]' && \
    uv pip freeze | grep -E '^(Jinja2|docutils|pytest|Pygments|setuptools)=='
RUN python -X dev -m pytest -rA tests/test_ext_autodoc_autoclass.py >/dev/null 2>&1 || true; \
    rm -rf /tmp/* && test -z "$(git status --porcelain)"
