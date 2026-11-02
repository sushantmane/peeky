# Bug environment for burntsushi__ripgrep-2576 (BurntSushi/ripgrep @ fed4fea217ab).
FROM peeky-study/base:1
ENV CARGO_TERM_COLOR=never
# Clone and keep only history up to the base commit, on a single main branch (one layer,
# so no later objects stay in the image).
RUN git clone -q https://github.com/BurntSushi/ripgrep.git /testbed && cd /testbed && \
    git checkout -q fed4fea217abbc502f2e823465de903c8f2b623d && git checkout -q -B main && \
    git remote remove origin && \
    git tag -l | xargs -r git tag -d >/dev/null && \
    git for-each-ref --format='%(refname)' refs/heads | grep -vx refs/heads/main | xargs -r -n1 git update-ref -d; \
    git reflog expire --expire=now --all && git gc --prune=now -q && \
    test -z "$(git rev-list --all --not fed4fea217abbc502f2e823465de903c8f2b623d)" && test "$(git rev-parse HEAD)" = fed4fea217abbc502f2e823465de903c8f2b623d
WORKDIR /testbed
RUN cargo fetch
ENV CARGO_NET_OFFLINE=true
RUN RUSTFLAGS=-Awarnings cargo test --package ripgrep --test integration --no-run
# Warm the test command once at base (failures are fine).
RUN (RUSTFLAGS=-Awarnings cargo test --package ripgrep --test integration -- regression) > /dev/null 2>&1; true
