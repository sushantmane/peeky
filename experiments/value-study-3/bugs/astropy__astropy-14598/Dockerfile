# astropy__astropy-14598 (SWE-bench Verified). astropy at the base commit on Python 3.11 (uv),
# following the SWE-bench spec for astropy 5.2: its pinned packages, setuptools 68.0.0 for the
# build (applied as a build constraint instead of editing pyproject.toml), then
# `pip install -e .[test]`, which compiles the C/Cython extensions. Unpinned packages resolve as of
# 2024-08-01, when the SWE-bench Verified environments were built. Python 3.12 is out: the pinned
# numpy 1.25.2 has no 3.12 build.
FROM peeky-study/base:1
ARG BASE=80c3854a5f4f4a6ab86c03d9db7854767fcd83c1
RUN git clone -q https://github.com/astropy/astropy.git /testbed && cd /testbed && \
    git checkout -q -B main $BASE && git remote remove origin && \
    git for-each-ref --format='%(refname)' | grep -vx refs/heads/main | xargs -r -n1 git update-ref -d && \
    git reflog expire --expire=now --all && git gc --prune=now -q && \
    test "$(git rev-parse HEAD)" = "$BASE" && \
    test "$(git rev-list --all | wc -l)" = "$(git rev-list HEAD | wc -l)" && \
    test -z "$(git fsck --unreachable --no-reflogs 2>/dev/null | head -1)"
WORKDIR /testbed
RUN uv venv -q --seed -p 3.11 /opt/venv
ENV VIRTUAL_ENV=/opt/venv PATH=/opt/venv/bin:$PATH
RUN export UV_EXCLUDE_NEWER=2024-08-01T00:00:00Z && \
    uv pip install -q attrs==23.1.0 exceptiongroup==1.1.3 execnet==2.0.2 hypothesis==6.82.6 \
        iniconfig==2.0.0 numpy==1.25.2 packaging==23.1 pluggy==1.3.0 psutil==5.9.5 \
        pyerfa==2.0.0.3 pytest-arraydiff==0.5.0 pytest-astropy-header==0.2.2 \
        pytest-astropy==0.10.0 pytest-cov==4.1.0 pytest-doctestplus==1.0.0 \
        pytest-filter-subpackage==0.1.2 pytest-mock==3.11.1 pytest-openfiles==0.5.0 \
        pytest-remotedata==0.4.0 pytest-xdist==3.3.1 pytest==7.4.0 PyYAML==6.0.1 \
        setuptools==68.0.0 sortedcontainers==2.4.0 tomli==2.0.1 && \
    echo 'setuptools==68.0.0' > /opt/build-constraints.txt && \
    uv pip install -b /opt/build-constraints.txt -e '.[test]' && \
    uv pip freeze | grep -E '^(numpy|pyerfa|pytest|setuptools|astropy)( @|==)'
RUN pytest -rA astropy/io/fits/tests/test_header.py >/dev/null 2>&1 || true; \
    test -z "$(git status --porcelain)"
