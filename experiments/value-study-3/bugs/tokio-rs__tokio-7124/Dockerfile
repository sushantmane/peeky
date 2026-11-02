# Bug environment for tokio-rs__tokio-7124 (tokio-rs/tokio @ fb7dec0e952d).
FROM peeky-study/base:1
ENV CARGO_TERM_COLOR=never
# Clone and keep only history up to the base commit, on a single main branch (one layer,
# so no later objects stay in the image).
RUN git clone -q https://github.com/tokio-rs/tokio.git /testbed && cd /testbed && \
    git checkout -q fb7dec0e952d0d796b98650998c44a21d6775564 && git checkout -q -B main && \
    git remote remove origin && \
    git tag -l | xargs -r git tag -d >/dev/null && \
    git for-each-ref --format='%(refname)' refs/heads | grep -vx refs/heads/main | xargs -r -n1 git update-ref -d; \
    git reflog expire --expire=now --all && git gc --prune=now -q && \
    test -z "$(git rev-list --all --not fb7dec0e952d0d796b98650998c44a21d6775564)" && test "$(git rev-parse HEAD)" = fb7dec0e952d0d796b98650998c44a21d6775564
WORKDIR /testbed
# The repo doesn't commit Cargo.lock. Multi-SWE-bench resolves dependencies afresh; this pins
# them: SWE-bench's lock file for tokio-rs__tokio-7139 (swebench/harness/constants/fixtures),
# completed by Cargo for this commit's manifests (Cargo.lock here; git ignores it in /testbed).
COPY Cargo.lock /testbed/Cargo.lock
RUN cargo fetch --locked
ENV CARGO_NET_OFFLINE=true
RUN cargo test --locked --test macros_select --test macros_test --test rt_basic --test rt_common --test rt_handle --test rt_metrics --test rt_threaded --test task_id --test task_join_set --test task_local_set --no-fail-fast --no-run
# Warm the test command once at base (failures are fine).
RUN (cargo test --test macros_select --test macros_test --test rt_basic --test rt_common --test rt_handle --test rt_metrics --test rt_threaded --test task_id --test task_join_set --test task_local_set --no-fail-fast) > /dev/null 2>&1; true
