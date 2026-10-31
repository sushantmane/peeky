# pallets__flask-5014 (SWE-bench Verified). Flask at the base commit, Python 3.11 (uv), the
# SWE-bench spec for flask 2.3: requirements/tests.txt plus its pinned packages, editable install.
# Python 3.12 is out: the pinned pytest 7.2.2 trips Flask's filterwarnings=error on 3.12 (ast.Str).
FROM peeky-study/base:1
ARG BASE=7ee9ceb71e868944a46e1ff00b506772a53a4f1d
RUN git clone -q https://github.com/pallets/flask.git /testbed && cd /testbed && \
    git checkout -q -B main $BASE && git remote remove origin && \
    git for-each-ref --format='%(refname)' | grep -vx refs/heads/main | xargs -r -n1 git update-ref -d && \
    git reflog expire --expire=now --all && git gc --prune=now -q && \
    test "$(git rev-parse HEAD)" = "$BASE" && \
    test "$(git rev-list --all | wc -l)" = "$(git rev-list HEAD | wc -l)" && \
    test -z "$(git fsck --unreachable --no-reflogs 2>/dev/null | head -1)"
RUN uv venv -q --seed -p 3.11 /opt/venv
ENV VIRTUAL_ENV=/opt/venv PATH=/opt/venv/bin:$PATH
RUN pip install -q -r requirements/tests.txt && \
    pip install -q setuptools==70.0.0 click==8.1.3 itsdangerous==2.1.2 Jinja2==3.1.2 \
        MarkupSafe==2.1.1 Werkzeug==2.3.7 && \
    pip install -q -e .
RUN pytest -rA tests/test_blueprints.py >/dev/null 2>&1 || true; \
    test -z "$(git status --porcelain)"
