# Bug environment for tokio-rs__tokio-4384 (tokio-rs/tokio @ 553cc3b194df).
FROM peeky-study/base:1
ENV CARGO_TERM_COLOR=never
# Clone and keep only history up to the base commit, on a single main branch (one layer,
# so no later objects stay in the image).
RUN git clone -q https://github.com/tokio-rs/tokio.git /testbed && cd /testbed && \
    git checkout -q 553cc3b194df875cac8736473e1f01cf3e40a660 && git checkout -q -B main && \
    git remote remove origin && \
    git tag -l | xargs -r git tag -d >/dev/null && \
    git for-each-ref --format='%(refname)' refs/heads | grep -vx refs/heads/main | xargs -r -n1 git update-ref -d; \
    git reflog expire --expire=now --all && git gc --prune=now -q && \
    test -z "$(git rev-list --all --not 553cc3b194df875cac8736473e1f01cf3e40a660)" && test "$(git rev-parse HEAD)" = 553cc3b194df875cac8736473e1f01cf3e40a660
WORKDIR /testbed
# The repo doesn't commit Cargo.lock; pin dependencies with SWE-bench's lock file
# (swebench/harness/constants/fixtures), with the entries this commit's manifests don't use
# dropped by Cargo (no version changes; Cargo.lock here; git ignores it in /testbed).
COPY Cargo.lock /testbed/Cargo.lock
RUN cargo fetch --locked
ENV CARGO_NET_OFFLINE=true
# The hidden test file is new; build tokio and its test dependencies with the test command's
# flags through an existing integration test, then run that test once (failures are fine).
RUN RUSTFLAGS=-Awarnings cargo test --locked --package tokio --features full --test sync_mpsc --no-fail-fast --no-run
RUN (RUSTFLAGS=-Awarnings cargo test --package tokio --features full --test sync_mpsc --no-fail-fast) > /dev/null 2>&1; true
