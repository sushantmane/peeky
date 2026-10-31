# mwaskom__seaborn-3187 (SWE-bench Verified). seaborn at the base commit on Python 3.11 (uv), with
# the SWE-bench spec for seaborn 0.12: its pinned packages, then `pip install -e .[dev]`; unpinned
# packages resolve as of 2024-08-01, when the SWE-bench Verified environments were built.
# One change from the spec: pandas 1.5.3 instead of 2.0.0, which removed the use_inf_as_null option
# this seaborn uses (most tests in the hidden tests' modules error on it). Python 3.12 is
# out: the pinned numpy 1.25.2 and matplotlib 3.7.2 have no 3.12 builds.
FROM peeky-study/base:1
ARG BASE=22cdfb0c93f8ec78492d87edb810f10cb7f57a31
RUN git clone -q https://github.com/mwaskom/seaborn.git /testbed && cd /testbed && \
    git checkout -q -B main $BASE && git remote remove origin && \
    git for-each-ref --format='%(refname)' | grep -vx refs/heads/main | xargs -r -n1 git update-ref -d && \
    git reflog expire --expire=now --all && git gc --prune=now -q && \
    test "$(git rev-parse HEAD)" = "$BASE" && \
    test "$(git rev-list --all | wc -l)" = "$(git rev-list HEAD | wc -l)" && \
    test -z "$(git fsck --unreachable --no-reflogs 2>/dev/null | head -1)"
RUN uv venv -q --seed -p 3.11 /opt/venv
ENV VIRTUAL_ENV=/opt/venv PATH=/opt/venv/bin:$PATH
RUN export UV_EXCLUDE_NEWER=2024-08-01T00:00:00Z && \
    uv pip install -q contourpy==1.1.0 cycler==0.11.0 fonttools==4.42.1 importlib-resources==6.0.1 \
        kiwisolver==1.4.5 matplotlib==3.7.2 numpy==1.25.2 packaging==23.1 pandas==1.5.3 \
        pillow==10.0.0 pyparsing==3.0.9 pytest python-dateutil==2.8.2 pytz==2023.3.post1 \
        scipy==1.11.2 six==1.16.0 tzdata==2023.1 zipp==3.16.2 && \
    uv pip install -q -e '.[dev]' && uv pip freeze | grep -E '^(numpy|pandas|matplotlib|pytest)=='
RUN pytest --no-header -rA tests/_core/test_plot.py tests/test_relational.py >/dev/null 2>&1 || true; \
    test -z "$(git status --porcelain)"
