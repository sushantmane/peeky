# Bug environment for nushell__nushell-10629 (nushell/nushell @ 399319476aa4).
FROM peeky-study/base:1
ENV CARGO_TERM_COLOR=never
# Clone and keep only history up to the base commit, on a single main branch (one layer,
# so no later objects stay in the image).
RUN git clone -q https://github.com/nushell/nushell.git /testbed && cd /testbed && \
    git checkout -q 399319476aa4decd7cced93488180d96e56bad64 && git checkout -q -B main && \
    git remote remove origin && \
    git tag -l | xargs -r git tag -d >/dev/null && \
    git for-each-ref --format='%(refname)' refs/heads | grep -vx refs/heads/main | xargs -r -n1 git update-ref -d; \
    git reflog expire --expire=now --all && git gc --prune=now -q && \
    test -z "$(git rev-list --all --not 399319476aa4decd7cced93488180d96e56bad64)" && test "$(git rev-parse HEAD)" = 399319476aa4decd7cced93488180d96e56bad64
WORKDIR /testbed
# rust-toolchain.toml pins Rust 1.70.0. The locked time 0.3.22 doesn't build on the base image's
# 1.96 (E0282, as on any Rust from 1.80), so the pinned toolchain is installed; rustup picks it
# from rust-toolchain.toml in /testbed.
RUN rustup toolchain install 1.70.0 --profile minimal && rustc -V
RUN cargo fetch
ENV CARGO_NET_OFFLINE=true
# The tests run target/debug/nu (assert_cmd), so the binary is built too.
RUN RUSTFLAGS=-Awarnings cargo build --package nu --bin nu && \
    RUSTFLAGS=-Awarnings cargo test --package nu --bin nu --no-run
# Warm the test command once at base (failures are fine).
RUN (RUSTFLAGS=-Awarnings cargo test --package nu --bin nu -- tests::test_type_check::) > /dev/null 2>&1; true
