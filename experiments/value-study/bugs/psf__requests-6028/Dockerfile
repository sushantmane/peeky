# psf__requests-6028 (SWE-bench Verified). requests at the base commit on Python 3.12, following
# the SWE-bench spec for requests 2.27 (pytest plus the package), with an editable install so
# changes in /testbed take effect without reinstalling. Packages resolve as of 2024-08-01, when the
# SWE-bench Verified environments were built.
FROM peeky-study/base:1
ARG BASE=0192aac24123735b3eaf9b08df46429bb770c283
RUN git clone -q https://github.com/psf/requests.git /testbed && cd /testbed && \
    git checkout -q -B main $BASE && git remote remove origin && \
    git for-each-ref --format='%(refname)' | grep -vx refs/heads/main | xargs -r -n1 git update-ref -d && \
    git reflog expire --expire=now --all && git gc --prune=now -q && \
    test "$(git rev-parse HEAD)" = "$BASE" && \
    test "$(git rev-list --all | wc -l)" = "$(git rev-list HEAD | wc -l)" && \
    test -z "$(git fsck --unreachable --no-reflogs 2>/dev/null | head -1)"
RUN python3.12 -m venv /opt/venv
ENV VIRTUAL_ENV=/opt/venv PATH=/opt/venv/bin:$PATH
RUN export UV_EXCLUDE_NEWER=2024-08-01T00:00:00Z && \
    uv pip install -q pytest setuptools -e . && \
    uv pip freeze | grep -E '^(pytest|urllib3|idna|charset-normalizer)=='
# The warm-up run leaves files in /tmp (extract_zipped_paths caches test_utils.py there); clear them.
RUN pytest -rA tests/test_utils.py >/dev/null 2>&1 || true; \
    rm -rf /tmp/* && test -z "$(git status --porcelain)"
