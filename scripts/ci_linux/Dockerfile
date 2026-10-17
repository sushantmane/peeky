# CI's Linux lanes on a laptop: Ubuntu 24.04 with LLVM 18 from the signed Ubuntu
# repositories (as the hosted lanes use), pinned Rust 1.96.0 and JDK 17. LLVM 20
# (same repositories) adds the lldb-dap with readMemory that Rust memory views need.
# python3-lldb-18 and -20 conflict (both own /usr/lib/python3/dist-packages/lldb),
# so lldb-20 and its own llvm-20 Python tree are unpacked instead (apt verifies the
# downloads against the signed index). GDB 15.1 checks the GDB DAP launch sequence
# that CUDA qualification drives cuda-gdb with (ADR 0007).
# Native architecture (arm64 on Apple Silicon; hosted CI is x86_64).
FROM ubuntu:24.04@sha256:534baea6a22c03a63003dbc8dbe78fe34bc0d7e595d9a9dc9834884ff530eb55
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update -qq && apt-get install -y --no-install-recommends \
      clang-18 lldb-18 llvm-18 liblldb-20 llvm-20 python3 python3-venv python3-pip \
      openjdk-17-jdk-headless curl ca-certificates git build-essential gdb && \
    cd /tmp && apt-get download lldb-20 python3-lldb-20 && dpkg -x lldb-20_*.deb / && \
    dpkg -x python3-lldb-20_*.deb /tmp/py20 && cp -a /tmp/py20/usr/lib/llvm-20 /usr/lib/ && \
    rm -rf /tmp/*.deb /tmp/py20 && test -z "$(/usr/bin/lldb-dap-20 --help 2>&1 >/dev/null)" && \
    rm -rf /var/lib/apt/lists/*
RUN curl -sSf https://sh.rustup.rs | sh -s -- -y --profile minimal --default-toolchain 1.96.0
ENV PATH=/root/.cargo/bin:$PATH
