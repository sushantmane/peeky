# Bug environment for clap-rs__clap-3391 (clap-rs/clap @ 7c79e76f2f55).
FROM peeky-study/base:1
ENV CARGO_TERM_COLOR=never
# Clone and keep only history up to the base commit, on a single main branch (one layer,
# so no later objects stay in the image).
RUN git clone -q https://github.com/clap-rs/clap.git /testbed && cd /testbed && \
    git checkout -q 7c79e76f2f55b1aab083fa201a86bbc8c8c35711 && git checkout -q -B main && \
    git remote remove origin && \
    git tag -l | xargs -r git tag -d >/dev/null && \
    git for-each-ref --format='%(refname)' refs/heads | grep -vx refs/heads/main | xargs -r -n1 git update-ref -d; \
    git reflog expire --expire=now --all && git gc --prune=now -q && \
    test -z "$(git rev-list --all --not 7c79e76f2f55b1aab083fa201a86bbc8c8c35711)" && test "$(git rev-parse HEAD)" = 7c79e76f2f55b1aab083fa201a86bbc8c8c35711
WORKDIR /testbed
# The repo has no Cargo.lock (git ignores it): cargo fetch resolves one, as Multi-SWE-bench's
# image did at its build.
RUN cargo fetch
ENV CARGO_NET_OFFLINE=true
# `--package clap` is ambiguous (criterion's clap 2), so the tests are named by target.
RUN RUSTFLAGS=-Awarnings cargo test --test builder --no-run
# Warm the test command once at base (failures are fine).
RUN (RUSTFLAGS=-Awarnings cargo test --test builder -- possible_values::) > /dev/null 2>&1; true
