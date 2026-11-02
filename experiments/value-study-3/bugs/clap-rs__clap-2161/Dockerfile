# Bug environment for clap-rs__clap-2161 (clap-rs/clap @ 4f90f3e4bb09).
FROM peeky-study/base:1
ENV CARGO_TERM_COLOR=never
# Clone and keep only history up to the base commit, on a single main branch (one layer,
# so no later objects stay in the image).
RUN git clone -q https://github.com/clap-rs/clap.git /testbed && cd /testbed && \
    git checkout -q 4f90f3e4bb09cc596aa10243fc1791fc574a5d0e && git checkout -q -B main && \
    git remote remove origin && \
    git tag -l | xargs -r git tag -d >/dev/null && \
    git for-each-ref --format='%(refname)' refs/heads | grep -vx refs/heads/main | xargs -r -n1 git update-ref -d; \
    git reflog expire --expire=now --all && git gc --prune=now -q && \
    test -z "$(git rev-list --all --not 4f90f3e4bb09cc596aa10243fc1791fc574a5d0e)" && test "$(git rev-parse HEAD)" = 4f90f3e4bb09cc596aa10243fc1791fc574a5d0e
WORKDIR /testbed
# The repo has no Cargo.lock (git ignores it): cargo fetch resolves one, as Multi-SWE-bench's
# image did at its build.
RUN cargo fetch
ENV CARGO_NET_OFFLINE=true
# `--package clap` is ambiguous (criterion's clap 2), so the tests are named by target; the
# workspace's default members are clap, clap_derive and clap_generate.
RUN RUSTFLAGS=-Awarnings cargo test --tests --no-run
# Warm the test command once at base (failures are fine).
RUN (RUSTFLAGS=-Awarnings cargo test --test opts) > /dev/null 2>&1; true
