# CI's Linux lanes on a laptop: Ubuntu 24.04 with LLVM 18 from the signed Ubuntu
# repositories (as the hosted lanes use), pinned Rust 1.96.0 and JDK 17. LLVM 20
# (same repositories) adds the lldb-dap with readMemory that Rust memory views need.
# Native architecture (arm64 on Apple Silicon; hosted CI is x86_64).
FROM ubuntu:24.04@sha256:534baea6a22c03a63003dbc8dbe78fe34bc0d7e595d9a9dc9834884ff530eb55
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update -qq && apt-get install -y --no-install-recommends \
      clang-18 lldb-18 llvm-18 lldb-20 llvm-20 python3 python3-venv python3-pip \
      openjdk-17-jdk-headless curl ca-certificates git build-essential && \
    rm -rf /var/lib/apt/lists/*
RUN curl -sSf https://sh.rustup.rs | sh -s -- -y --profile minimal --default-toolchain 1.96.0
ENV PATH=/root/.cargo/bin:$PATH
