# Round 2's Peeky images: both Peeky arms (`peeky` and `peeky-first`) run in them. Built by
# harness/build_images.sh, which runs round 1's build script with this file. That script stages
# the context: a wheel built from a Peeky commit, its agent docs, and prepare.sh beside this file.
#
#   target `dist`: peeky-study/peeky-dist-r2:<sha>, the base image plus Peeky. Network allowed
#                  at build: pip fetches debugpy and `peeky setup-java` fetches the adapter.
#   target `bug`:  peeky-study/<id>:peeky-r2, a bug's control image plus what `dist` installed,
#                  then prepare.sh <id>: the project's Peeky build (C and C++), its vendored
#                  crates (Rust), and /opt/peeky/docs/PROJECT.md, which says what it prepared.
#
# What differs from round 1's peeky.Dockerfile:
# - `peeky setup-java` runs with JDK 17 first on PATH, so the adapter bundle runs on JDK 17 and
#   debugs the JDK 11, 17 and 21 projects alike (#449; the guide's Java section).
# - prepare.sh, run in the bug stage, with network.
ARG BUG_IMAGE=peeky-study/base:1
ARG DIST_IMAGE=peeky-study/peeky-dist-r2:latest

FROM peeky-study/base:1 AS dist
ARG PEEKY_SHA=unknown
COPY wheel/ /opt/peeky/wheel/
COPY docs/ /opt/peeky/docs/
RUN python3.12 -m venv /opt/peeky/venv && \
    /opt/peeky/venv/bin/pip install --no-cache-dir -q /opt/peeky/wheel/*.whl && \
    rm -rf /opt/peeky/wheel && \
    for tool in peeky peeky-probe peeky-worker; do ln -s /opt/peeky/venv/bin/$tool /usr/local/bin/$tool; done && \
    echo "$PEEKY_SHA" > /opt/peeky/SHA
ENV PEEKY_NATIVE_DAP=/usr/bin/lldb-dap-18 \
    PEEKY_NATIVE_DWARFDUMP=/usr/bin/llvm-dwarfdump-18 \
    PEEKY_NATIVE_CC=/usr/bin/clang-18 \
    PEEKY_NATIVE_CXX=/usr/bin/clang++-18 \
    PEEKY_RUST_DAP=/usr/bin/lldb-dap-18 \
    PEEKY_RUST_DWARFDUMP=/usr/bin/llvm-dwarfdump-18 \
    PEEKY_RUST_LINKER=/usr/bin/clang-18 \
    PEEKY_RUST_CARGO=/root/.rustup/toolchains/1.96.0-aarch64-unknown-linux-gnu/bin/cargo \
    PEEKY_RUST_RUSTC=/root/.rustup/toolchains/1.96.0-aarch64-unknown-linux-gnu/bin/rustc
RUN PATH=/usr/lib/jvm/java-17-openjdk-arm64/bin:$PATH peeky setup-java && \
    chmod 700 /root/.peeky && peeky --version
LABEL peeky-study.peeky-sha=$PEEKY_SHA peeky-study.round=2

FROM ${DIST_IMAGE} AS built-dist

FROM ${BUG_IMAGE} AS bug
ARG BUG_ID
COPY --from=built-dist /opt/peeky /opt/peeky
COPY --from=built-dist /root/.peeky /root/.peeky
RUN chown -R root:root /root/.peeky && chmod 700 /root/.peeky && \
    for tool in peeky peeky-probe peeky-worker; do ln -sf /opt/peeky/venv/bin/$tool /usr/local/bin/$tool; done
ENV PEEKY_NATIVE_DAP=/usr/bin/lldb-dap-18 \
    PEEKY_NATIVE_DWARFDUMP=/usr/bin/llvm-dwarfdump-18 \
    PEEKY_NATIVE_CC=/usr/bin/clang-18 \
    PEEKY_NATIVE_CXX=/usr/bin/clang++-18 \
    PEEKY_RUST_DAP=/usr/bin/lldb-dap-18 \
    PEEKY_RUST_DWARFDUMP=/usr/bin/llvm-dwarfdump-18 \
    PEEKY_RUST_LINKER=/usr/bin/clang-18 \
    PEEKY_RUST_CARGO=/root/.rustup/toolchains/1.96.0-aarch64-unknown-linux-gnu/bin/cargo \
    PEEKY_RUST_RUSTC=/root/.rustup/toolchains/1.96.0-aarch64-unknown-linux-gnu/bin/rustc
COPY prepare.sh /opt/peeky/study/prepare.sh
RUN bash /opt/peeky/study/prepare.sh "$BUG_ID"
ARG PEEKY_SHA=unknown
LABEL peeky-study.peeky-sha=$PEEKY_SHA peeky-study.round=2 peeky-study.bug=$BUG_ID
