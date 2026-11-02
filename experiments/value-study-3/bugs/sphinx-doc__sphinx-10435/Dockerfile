# sphinx-doc__sphinx-10435 (SWE-bench Verified). Sphinx at the base commit on Python 3.12,
# following the SWE-bench spec for sphinx 5.0: Jinja2 3.0.3 and `pip install -e .[test]`.
# Packages resolve as of 2024-08-01, when the SWE-bench Verified environments were built.
# Tests run with tox.ini's settings, without tox (see eval.sh).
FROM peeky-study/base:1
ARG BASE=f1061c012e214f16fd8790dec3c283d787e3daa8
RUN git clone -q https://github.com/sphinx-doc/sphinx.git /testbed && cd /testbed && \
    git checkout -q -B main $BASE && git remote remove origin && \
    git for-each-ref --format='%(refname)' | grep -vx refs/heads/main | xargs -r -n1 git update-ref -d && \
    git reflog expire --expire=now --all && git gc --prune=now -q && \
    test "$(git rev-parse HEAD)" = "$BASE" && \
    test "$(git rev-list --all | wc -l)" = "$(git rev-list HEAD | wc -l)" && \
    test -z "$(git fsck --unreachable --no-reflogs 2>/dev/null | head -1)"
RUN python3.12 -m venv /opt/venv
ENV VIRTUAL_ENV=/opt/venv PATH=/opt/venv/bin:$PATH
RUN export UV_EXCLUDE_NEWER=2024-08-01T00:00:00Z && \
    uv pip install -q Jinja2==3.0.3 -e '.[test]' && \
    uv pip freeze | grep -E '^(Jinja2|docutils|pytest|Pygments|setuptools)=='
RUN PYTHONWARNINGS=all python -X dev -X warn_default_encoding -m pytest -rA tests/test_build_latex.py >/dev/null 2>&1 || true; \
    rm -rf /tmp/* && test -z "$(git status --porcelain)"
