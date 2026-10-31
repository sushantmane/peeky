# The Peeky arm's images. Built by harness/build_peeky_images.sh, which stages the context
# (a wheel built from the Peeky checkout, and its agent docs) and passes the build arguments.
#
#   target `dist`: peeky-study/peeky-dist:<sha>, the base image plus Peeky. Network allowed at
#                  build: pip fetches debugpy and `peeky setup-java` fetches the Java adapter.
#   target `bug`:  peeky-study/<id>:peeky, a bug's control image plus what `dist` installed,
#                  copied over: /opt/peeky (venv and docs) and /root/.peeky (the Java adapter).
#
# The two stages set the same ENV: the tool paths the CI lanes give Peeky
# (scripts/ci_linux/run_lanes.sh), which are also what it would find on PATH in this image.
ARG BUG_IMAGE=peeky-study/base:1
ARG DIST_IMAGE=peeky-study/peeky-dist:latest

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
RUN peeky setup-java && chmod 700 /root/.peeky && peeky --version
LABEL peeky-study.peeky-sha=$PEEKY_SHA

FROM ${DIST_IMAGE} AS built-dist

FROM ${BUG_IMAGE} AS bug
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
ARG PEEKY_SHA=unknown
LABEL peeky-study.peeky-sha=$PEEKY_SHA
