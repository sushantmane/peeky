# django__django-11087 (SWE-bench Verified). Django at the base commit on Python 3.11 (uv), following the
# SWE-bench spec for Django 3.0: tests/requirements/py3.txt (without pylibmc and pywatchman, which
# need libmemcached and a native build), `pip install -e .`, and the en_US.UTF-8 locale.
# Packages resolve as of 2024-08-01, when the SWE-bench Verified environments were built.
FROM peeky-study/base:1
ARG BASE=8180ffba21bf10f4be905cb0d4890dc2bcff2788
RUN apt-get update -qq && apt-get install -y -qq --no-install-recommends locales >/dev/null && \
    rm -rf /var/lib/apt/lists/* && sed -i '/en_US.UTF-8/s/^# //g' /etc/locale.gen && locale-gen >/dev/null
ENV LANG=en_US.UTF-8 LANGUAGE=en_US:en LC_ALL=en_US.UTF-8
RUN git clone -q https://github.com/django/django.git /testbed && cd /testbed && \
    git checkout -q -B main $BASE && git remote remove origin && \
    git for-each-ref --format='%(refname)' | grep -vx refs/heads/main | xargs -r -n1 git update-ref -d && \
    git reflog expire --expire=now --all && git gc --prune=now -q && \
    test "$(git rev-parse HEAD)" = "$BASE" && \
    test "$(git rev-list --all | wc -l)" = "$(git rev-list HEAD | wc -l)" && \
    test -z "$(git fsck --unreachable --no-reflogs 2>/dev/null | head -1)"
RUN uv venv -q --seed -p 3.11 /opt/venv
ENV VIRTUAL_ENV=/opt/venv PATH=/opt/venv/bin:$PATH
RUN export UV_EXCLUDE_NEWER=2024-08-01T00:00:00Z && \
    grep -vE '^(pylibmc|pywatchman)' tests/requirements/py3.txt > /opt/requirements.txt && \
    uv pip install -q -r /opt/requirements.txt && uv pip install -q setuptools -e . && \
    uv pip freeze | grep -iE '^(django|sqlparse|pytz)( @|==)'
RUN ./tests/runtests.py --verbosity 2 --settings=test_sqlite --parallel 1 delete >/dev/null 2>&1 || true; \
    rm -rf /tmp/* && test -z "$(git status --porcelain)"
