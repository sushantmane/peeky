# astropy__astropy-7606 (SWE-bench Verified). astropy (3.1.dev) at the base commit on Python 3.11
# (uv). SWE-bench's spec for this instance (astropy "1.3") is Python 3.6 with numpy 1.16.0 and
# pytest 3.3.1, none of which install or run on 3.11; so the image uses the recipe of the first
# pass's astropy images (astropy__astropy-8707): the pins of SWE-bench's spec for astropy 3.x,
# with pytest 7.1.3 (from 7.2 nose-style setup() methods warn, and this astropy's conftest turns
# DeprecationWarnings into errors), then `pip install -e .`, a legacy `setup.py develop` through
# the astropy_helpers submodule that compiles the C/Cython extensions (Cython below 3, Jinja2 for
# the ERFA wrapper). Unpinned packages resolve as of 2024-08-01, when the SWE-bench Verified
# environments were built. Python 3.12 is out: astropy_helpers needs distutils.
FROM peeky-study/base:1
ARG BASE=3cedd79e6c121910220f8e6df77c54a0b344ea94
RUN git clone -q https://github.com/astropy/astropy.git /testbed && cd /testbed && \
    git checkout -q -B main $BASE && git submodule -q update --init astropy_helpers && \
    git remote remove origin && \
    git for-each-ref --format='%(refname)' | grep -vx refs/heads/main | xargs -r -n1 git update-ref -d && \
    git reflog expire --expire=now --all && git gc --prune=now -q && \
    (cd astropy_helpers && git remote remove origin && \
     git for-each-ref --format='%(refname)' | xargs -r -n1 git update-ref -d && \
     git reflog expire --expire=now --all && git gc --prune=now -q) && \
    test "$(git rev-parse HEAD)" = "$BASE" && \
    test "$(git rev-list --all | wc -l)" = "$(git rev-list HEAD | wc -l)" && \
    test -z "$(git fsck --unreachable --no-reflogs 2>/dev/null | head -1)" && \
    test -z "$(git status --porcelain)"
WORKDIR /testbed
RUN uv venv -q --seed -p 3.11 /opt/venv
ENV VIRTUAL_ENV=/opt/venv PATH=/opt/venv/bin:$PATH SETUPTOOLS_USE_DISTUTILS=stdlib
RUN export UV_EXCLUDE_NEWER=2024-08-01T00:00:00Z && \
    uv pip install -q attrs==23.1.0 exceptiongroup==1.1.3 execnet==2.0.2 hypothesis==6.82.6 \
        iniconfig==2.0.0 numpy==1.25.2 packaging==23.1 pluggy==1.3.0 psutil==5.9.5 \
        pyerfa==2.0.0.3 pytest-arraydiff==0.5.0 pytest-astropy-header==0.2.2 \
        pytest-astropy==0.10.0 pytest-cov==4.1.0 pytest-doctestplus==1.0.0 \
        pytest-filter-subpackage==0.1.2 pytest-mock==3.11.1 pytest-openfiles==0.5.0 \
        pytest-remotedata==0.4.0 pytest-xdist==3.3.1 pytest==7.1.3 PyYAML==6.0.1 \
        setuptools==68.0.0 sortedcontainers==2.4.0 tomli==2.0.1 'cython<3' jinja2 && \
    python -m pip install -q --no-build-isolation --no-deps -e . && \
    uv pip freeze | grep -E '^(numpy|pyerfa|pytest|setuptools|astropy|cython|Cython)( @|==|$)' ; \
    python -c 'import astropy, astropy.units; print(astropy.__version__)'
RUN pytest -rA astropy/units/tests/test_units.py >/dev/null 2>&1 || true; \
    test -z "$(git status --porcelain)"
