# Bug environment for sharkdp__bat-3108 (sharkdp/bat @ 2be3a14a7e2c).
FROM peeky-study/base:1
ENV CARGO_TERM_COLOR=never
# Clone and keep only history up to the base commit, on a single main branch (one layer,
# so no later objects stay in the image).
RUN git clone -q https://github.com/sharkdp/bat.git /testbed && cd /testbed && \
    git checkout -q 2be3a14a7e2c74360765a5fa213750f727f2c5bb && git checkout -q -B main && \
    git remote remove origin && \
    git tag -l | xargs -r git tag -d >/dev/null && \
    git for-each-ref --format='%(refname)' refs/heads | grep -vx refs/heads/main | xargs -r -n1 git update-ref -d; \
    git reflog expire --expire=now --all && git gc --prune=now -q && \
    test -z "$(git rev-list --all --not 2be3a14a7e2c74360765a5fa213750f727f2c5bb)" && test "$(git rev-parse HEAD)" = 2be3a14a7e2c74360765a5fa213750f727f2c5bb
WORKDIR /testbed
RUN cargo fetch
ENV CARGO_NET_OFFLINE=true
RUN RUSTFLAGS=-Awarnings cargo test --package bat --test integration_tests pag --no-run
# Warm the test command once at base (failures are fine).
RUN (RUSTFLAGS=-Awarnings cargo test --package bat --test integration_tests pag) > /dev/null 2>&1; true
