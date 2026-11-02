# Bug environment for burntsushi__ripgrep-2295 (BurntSushi/ripgrep @ 1d35859861fa).
FROM peeky-study/base:1
ENV CARGO_TERM_COLOR=never
# Clone and keep only history up to the base commit, on a single main branch (one layer,
# so no later objects stay in the image).
RUN git clone -q https://github.com/BurntSushi/ripgrep.git /testbed && cd /testbed && \
    git checkout -q 1d35859861fa4710cee94cf0e0b2e114b152b946 && git checkout -q -B main && \
    git remote remove origin && \
    git tag -l | xargs -r git tag -d >/dev/null && \
    git for-each-ref --format='%(refname)' refs/heads | grep -vx refs/heads/main | xargs -r -n1 git update-ref -d; \
    git reflog expire --expire=now --all && git gc --prune=now -q && \
    test -z "$(git rev-list --all --not 1d35859861fa4710cee94cf0e0b2e114b152b946)" && test "$(git rev-parse HEAD)" = 1d35859861fa4710cee94cf0e0b2e114b152b946
WORKDIR /testbed
RUN cargo fetch
ENV CARGO_NET_OFFLINE=true
RUN RUSTFLAGS=-Awarnings cargo test --package ripgrep --test integration --no-run
# Warm the test command once at base (failures are fine).
RUN (RUSTFLAGS=-Awarnings cargo test --package ripgrep --test integration -- regression::) > /dev/null 2>&1; true
