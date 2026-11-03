# scikit-learn__scikit-learn-26194 (SWE-bench Verified). scikit-learn (1.3.dev) at the base commit
# on Python 3.11 (uv), following the SWE-bench spec for scikit-learn 1.3: its packages (Cython
# 3.0.10, pandas below 2, matplotlib below 3.9, pytest, joblib, threadpoolctl, setuptools), then
# `pip install -v --no-use-pep517 --no-build-isolation -e .`, which cythonizes and compiles the
# extensions in place (with OpenMP, from gcc). The spec's numpy 1.19.2 and scipy 1.5.2 have no
# Python 3.11 build, so they are the oldest that do: numpy 1.23.2 (what the checkout's
# pyproject.toml, through oldest-supported-numpy, builds against on 3.11) and scipy 1.9.3.
# Unpinned packages resolve as of 2024-08-01, when the SWE-bench Verified environments were built
# (pinned where the venv seeds newer: pip 24.2, setuptools 72.1.0, wheel 0.43.0; later pips have
# no --no-use-pep517).
# Python 3.12 is out: pandas below 2 has no 3.12 build.
FROM peeky-study/base:1
ARG BASE=e886ce4e1444c61b865e7839c9cff5464ee20ace
RUN git clone -q https://github.com/scikit-learn/scikit-learn.git /testbed && cd /testbed && \
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
    uv pip install -q numpy==1.23.2 scipy==1.9.3 cython==3.0.10 pytest 'pandas<2.0.0' \
        'matplotlib<3.9.0' setuptools==72.1.0 joblib threadpoolctl wheel==0.43.0 pip==24.2 && \
    SKLEARN_BUILD_PARALLEL=4 python -m pip install -q --no-use-pep517 --no-build-isolation -e . && \
    uv pip freeze | grep -iE '^(numpy|scipy|cython|pytest|pandas|matplotlib|setuptools|joblib|threadpoolctl|scikit-learn)( @|==|$)' && \
    python -c 'import sklearn; sklearn.show_versions()'
RUN pytest -rA sklearn/metrics/tests/test_ranking.py | tail -3 || true; \
    git status --porcelain; test -z "$(git status --porcelain)"
