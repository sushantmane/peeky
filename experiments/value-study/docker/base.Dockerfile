# The study's base image: the CI lanes' toolchains (scripts/ci_linux/Dockerfile: Ubuntu 24.04,
# clang 18, GCC 13, LLDB 18 and 20, GDB, JDK 17, Python 3.12, Rust 1.96) plus the build tools the
# selected projects need. Both arms use it; only the peeky image adds Peeky.
# Build: docker build -t peeky-study/base:1 -f base.Dockerfile ../../../scripts/ci_linux
FROM peeky-ci-linux:latest
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update -qq && apt-get install -y --no-install-recommends \
      openjdk-21-jdk-headless openjdk-11-jdk-headless maven ant \
      cmake ninja-build autoconf automake libtool pkg-config bison flex \
      python3.12-dev tcl tcl-dev libssl-dev zlib1g-dev \
      unzip zip xz-utils file less vim-tiny procps psmisc && \
    rm -rf /var/lib/apt/lists/*
# uv provides CPython 3.11 for projects that don't run on 3.12.
RUN curl -LsSf https://astral.sh/uv/0.9.5/install.sh | env UV_INSTALL_DIR=/usr/local/bin sh && \
    uv python install 3.11 && \
    git config --global user.email study@example.invalid && git config --global user.name study && \
    git config --global --add safe.directory '*'
# JDK 17 stays the default `java`; projects that need another select it in their own image.
ENV JAVA_HOME=/usr/lib/jvm/java-17-openjdk-arm64
WORKDIR /testbed
