# Bug environment for burntsushi__ripgrep-2209 (burntsushi/ripgrep @ 4dc6c73c5a92).
FROM peeky-study/base:1
ENV CARGO_TERM_COLOR=never
# Clone and keep only history up to the base commit, on a single main branch (one layer,
# so no later objects stay in the image).
RUN git clone -q https://github.com/burntsushi/ripgrep.git /testbed && cd /testbed && \
    git checkout -q 4dc6c73c5a9203c5a8a89ce2161feca542329812 && git checkout -q -B main && \
    git remote remove origin && \
    git tag -l | xargs -r git tag -d >/dev/null && \
    git for-each-ref --format='%(refname)' refs/heads | grep -vx refs/heads/main | xargs -r -n1 git update-ref -d; \
    git reflog expire --expire=now --all && git gc --prune=now -q && \
    test -z "$(git rev-list --all --not 4dc6c73c5a9203c5a8a89ce2161feca542329812)" && test "$(git rev-parse HEAD)" = 4dc6c73c5a9203c5a8a89ce2161feca542329812
WORKDIR /testbed
RUN cargo fetch
ENV CARGO_NET_OFFLINE=true
RUN RUSTFLAGS=-Awarnings cargo test --package ripgrep --test integration --no-run
# Warm the test command once at base (failures are fine).
RUN (RUSTFLAGS=-Awarnings cargo test --package ripgrep --test integration -- regression::r2208 --exact) > /dev/null 2>&1; true
