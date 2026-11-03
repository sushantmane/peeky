# sympy__sympy-22080 (SWE-bench Verified). sympy at the base commit on Python 3.12, following the
# SWE-bench spec for sympy 1.10 (mpmath 1.3.0, flake8 and flake8-comprehensions, then
# `pip install -e .`). Unpinned packages resolve as of 2024-08-01, when the SWE-bench Verified
# environments were built.
FROM peeky-study/base:1
ARG BASE=3f8c8c2377cb8e0daaf8073e8d03ac7d87580813
RUN git clone -q https://github.com/sympy/sympy.git /testbed && cd /testbed && \
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
    uv pip install -q mpmath==1.3.0 flake8 flake8-comprehensions setuptools && \
    uv pip install -e . && \
    uv pip freeze | grep -E '^(mpmath|sympy|setuptools)( @|==)'
RUN PYTHONWARNINGS='ignore::UserWarning,ignore::SyntaxWarning' bin/test -C --verbose \
        sympy/codegen/tests/test_rewriting.py sympy/printing/tests/test_pycode.py \
        sympy/utilities/tests/test_lambdify.py >/dev/null 2>&1 || true; \
    test -z "$(git status --porcelain)"
