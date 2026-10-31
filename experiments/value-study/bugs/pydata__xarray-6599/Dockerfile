# pydata__xarray-6599 (SWE-bench Verified). xarray at the base commit on Python 3.11 (uv). The
# SWE-bench spec for xarray 2022.03 builds a conda env from ci/requirements/environment.yml plus
# pinned packages; there's no conda here, so the image installs the spec's pins (numpy 1.23.5
# instead of 1.23.0, which has no 3.11 build) and the pip-installable test dependencies from
# environment.yml that have Linux arm64 wheels, then `pip install -e .`. Unpinned packages resolve
# as of 2024-08-01, when the SWE-bench Verified environments were built. Python 3.12 is out: the
# pinned numpy 1.23 and scipy 1.11.1 have no 3.12 builds.
FROM peeky-study/base:1
ARG BASE=6bb2b855498b5c68d7cca8cceb710365d58e6048
RUN git clone -q https://github.com/pydata/xarray.git /testbed && cd /testbed && \
    git checkout -q -B main $BASE && git remote remove origin && \
    git for-each-ref --format='%(refname)' | grep -vx refs/heads/main | xargs -r -n1 git update-ref -d && \
    git reflog expire --expire=now --all && git gc --prune=now -q && \
    test "$(git rev-parse HEAD)" = "$BASE" && \
    test "$(git rev-list --all | wc -l)" = "$(git rev-list HEAD | wc -l)" && \
    test -z "$(git fsck --unreachable --no-reflogs 2>/dev/null | head -1)"
RUN uv venv -q --seed -p 3.11 /opt/venv
ENV VIRTUAL_ENV=/opt/venv PATH=/opt/venv/bin:$PATH
RUN export UV_EXCLUDE_NEWER=2024-08-01T00:00:00Z && \
    printf '%s\n' numpy==1.23.5 packaging==23.1 pandas==1.5.3 pytest==7.4.0 python-dateutil==2.8.2 \
        pytz==2023.3 six==1.16.0 scipy==1.11.1 setuptools==68.0.0 dask==2022.8.1 > /opt/constraints.txt && \
    uv pip install -q -c /opt/constraints.txt numpy packaging pandas pytest python-dateutil pytz six \
        scipy setuptools 'dask[array]' distributed bottleneck cftime toolz hypothesis pytest-env \
        pytest-xdist pytest-cov matplotlib netCDF4 zarr pint pooch && \
    uv pip install -q -c /opt/constraints.txt -e . && \
    uv pip freeze | grep -E '^(numpy|pandas|dask|pytest|xarray)( @|==)'
RUN pytest -rA xarray/tests/test_computation.py >/dev/null 2>&1 || true; \
    test -z "$(git status --porcelain)"
