# matplotlib__matplotlib-21568 (SWE-bench Verified). matplotlib (3.5.0.dev) at the base commit on
# Python 3.11 (uv), following the SWE-bench spec for matplotlib 3.4 (Python 3.8): the checkout's
# test requirements (requirements/testing/all.txt and extra.txt; the docs and flake8 files that
# dev-requirements.txt also lists are left out), pytest and ipython, then `pip install -e .`,
# which builds the C extensions against the bundled FreeType 2.6.1 and Qhull (downloaded by
# setup.py at build time, as there). ghostscript (pdf/eps comparisons) and libcairo2 (the Cairo
# backend) come from apt, as in the first pass's matplotlib images, instead of the spec's
# texlive/imagemagick install. Unpinned packages resolve as of 2024-08-01, when the SWE-bench
# Verified environments were built; numpy is pinned to 1.24.4, what the spec's Python 3.8
# resolves to (on 3.11 it would resolve to numpy 2, which this matplotlib can't build against),
# and setuptools (72.1.0), wheel (0.43.0) and pip (24.2) to their versions of that date, as the
# venv seeds newer ones. Installed without build isolation (the checkout has no pyproject.toml).
# Python 3.11 rather than 3.12: this matplotlib's build needs distutils (removed in 3.12).
FROM peeky-study/base:1
ARG BASE=f0632c0fc7339f68e992ed63ae4cfac76cd41aad
RUN apt-get update -q && DEBIAN_FRONTEND=noninteractive apt-get install -y -q --no-install-recommends \
        ghostscript libcairo2 pkg-config && rm -rf /var/lib/apt/lists/*
RUN git clone -q https://github.com/matplotlib/matplotlib.git /testbed && cd /testbed && \
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
    uv pip install -q numpy==1.24.4 setuptools==72.1.0 wheel==0.43.0 pip==24.2 \
        -r requirements/testing/all.txt -r requirements/testing/extra.txt pytest ipython \
        certifi setuptools_scm setuptools_scm_git_archive cycler fonttools kiwisolver \
        packaging pillow python-dateutil cairocffi && \
    python -m pip install -q --no-build-isolation -e . && \
    uv pip freeze | grep -iE '^(numpy|pytest|setuptools|setuptools-scm|matplotlib|pillow|pyparsing|pandas)( @|==)' && \
    python -c 'import matplotlib; print(matplotlib.__version__); import matplotlib.ft2font'
RUN pytest -rA lib/matplotlib/tests/test_dates.py | tail -3 || true; \
    rm -rf result_images && git status --porcelain && test -z "$(git status --porcelain)"
