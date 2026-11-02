# matplotlib__matplotlib-24870 (SWE-bench Verified). matplotlib at the base commit on Python 3.11
# (uv), following the SWE-bench spec for matplotlib 3.6: its pinned packages, then
# `pip install -e .`, which builds the C extensions against the bundled FreeType 2.6.1 and Qhull
# (downloaded by setup.py at build time, as there). environment.yml's conda packages (as of the
# environment setup commit) are replaced by their PyPI test subset (GUI toolkits, docs and lint
# tools left out); ghostscript (pdf/eps comparisons) and libcairo2 (the Cairo backend, via
# cairocffi) come from apt, as SWE-bench's texlive/imagemagick install brought them. The build
# requirements are installed first and the checkout is built without isolation. Unpinned
# packages resolve as of 2024-08-01, when the SWE-bench Verified environments were built.
# Python 3.12 is out: the pinned numpy 1.25.2 has no 3.12 build.
FROM peeky-study/base:1
ARG BASE=6091437be9776139d3672cde28a19cbe6c09dcd5
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
    uv pip install -q contourpy==1.1.0 cycler==0.11.0 fonttools==4.42.1 ghostscript \
        kiwisolver==1.4.5 numpy==1.25.2 packaging==23.1 pillow==10.0.0 pikepdf pyparsing==3.0.9 \
        python-dateutil==2.8.2 six==1.16.0 setuptools==68.1.2 setuptools-scm==7.1.0 \
        typing-extensions==4.7.1 \
        certifi 'pybind11>=2.6' wheel cairocffi 'importlib-resources>=3.2.0' ipython psutil \
        pandas pytz tornado pyyaml coverage 'pytest!=4.6.0,!=5.4.0' pytest-cov \
        pytest-rerunfailures pytest-timeout pytest-xdist && \
    uv pip install --no-build-isolation -e . && \
    uv pip freeze | grep -E '^(numpy|pytest|setuptools|matplotlib|pillow|pandas|cairocffi)( @|==)'
RUN pytest -rA lib/matplotlib/tests/test_contour.py >/dev/null 2>&1 || true; \
    rm -rf result_images && test -z "$(git status --porcelain)"
