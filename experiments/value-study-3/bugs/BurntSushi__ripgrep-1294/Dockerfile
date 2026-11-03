# Bug environment for BurntSushi__ripgrep-1294 (BurntSushi/ripgrep @ 392682d35296).
FROM peeky-study/base:1
ENV CARGO_TERM_COLOR=never
# Clone and keep only history up to the base commit, on a single main branch (one layer,
# so no later objects stay in the image).
RUN git clone -q https://github.com/BurntSushi/ripgrep.git /testbed && cd /testbed && \
    git checkout -q 392682d35296bda5c0d0cccf43bae55be3d084df && git checkout -q -B main && \
    git remote remove origin && \
    git tag -l | xargs -r git tag -d >/dev/null && \
    git for-each-ref --format='%(refname)' refs/heads | grep -vx refs/heads/main | xargs -r -n1 git update-ref -d; \
    git reflog expire --expire=now --all && git gc --prune=now -q && \
    test -z "$(git rev-list --all --not 392682d35296bda5c0d0cccf43bae55be3d084df)" && test "$(git rev-parse HEAD)" = 392682d35296bda5c0d0cccf43bae55be3d084df
WORKDIR /testbed
RUN cargo fetch --locked
ENV CARGO_NET_OFFLINE=true
RUN RUSTFLAGS=-Awarnings cargo test --locked --package ripgrep --test integration --no-run
# Warm the test command once at base (failures are fine).
RUN (RUSTFLAGS=-Awarnings cargo test --package ripgrep --test integration -- misc::) > /dev/null 2>&1; true
