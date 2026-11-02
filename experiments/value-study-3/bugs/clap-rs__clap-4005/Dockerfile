# Bug environment for clap-rs__clap-4005 (clap-rs/clap @ edc7ac359fda).
FROM peeky-study/base:1
ENV CARGO_TERM_COLOR=never
# Clone and keep only history up to the base commit, on a single main branch (one layer,
# so no later objects stay in the image).
RUN git clone -q https://github.com/clap-rs/clap.git /testbed && cd /testbed && \
    git checkout -q edc7ac359fda218f82bedbdfe519ca57b8fe155c && git checkout -q -B main && \
    git remote remove origin && \
    git tag -l | xargs -r git tag -d >/dev/null && \
    git for-each-ref --format='%(refname)' refs/heads | grep -vx refs/heads/main | xargs -r -n1 git update-ref -d; \
    git reflog expire --expire=now --all && git gc --prune=now -q && \
    test -z "$(git rev-list --all --not edc7ac359fda218f82bedbdfe519ca57b8fe155c)" && test "$(git rev-parse HEAD)" = edc7ac359fda218f82bedbdfe519ca57b8fe155c
WORKDIR /testbed
RUN cargo fetch
ENV CARGO_NET_OFFLINE=true
# The tests are named by target, from the root package (clap).
RUN RUSTFLAGS=-Awarnings cargo test --test builder --no-run
# Warm the test command once at base (failures are fine).
RUN (RUSTFLAGS=-Awarnings cargo test --test builder -- require::) > /dev/null 2>&1; true
