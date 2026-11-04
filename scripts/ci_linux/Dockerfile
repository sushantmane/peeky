# CI's Linux lanes, on GitHub Actions and on a laptop: Ubuntu 24.04 with LLVM 18 from the signed
# Ubuntu repositories, pinned Rust 1.96.0, JDK 17, and CPython 3.11 and 3.13. LLVM 20
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
# rustup-init 1.29.2, checked against its published SHA-256 for the architecture before it runs.
RUN true \
    && case "$(dpkg --print-architecture)" in \
      amd64) triple=x86_64-unknown-linux-gnu \
        sum=5be191e8c70a5007074254cb74d78dbfdd25cd5a61c27de7b8e5410122ec24f4 ;; \
      arm64) triple=aarch64-unknown-linux-gnu \
        sum=a9c168dbea81a84671c6b7a5c1795cb0eb699020110d107ddfca427bd797e697 ;; \
      *) echo "no pinned rustup-init for $(dpkg --print-architecture)" >&2; exit 1 ;; \
    esac \
    && curl -sSf -o /tmp/rustup-init \
      "https://static.rust-lang.org/rustup/archive/1.29.2/$triple/rustup-init" \
    && echo "$sum  /tmp/rustup-init" | sha256sum -c - \
    && chmod +x /tmp/rustup-init \
    && /tmp/rustup-init -y --profile minimal --default-toolchain 1.96.0 \
    && rm /tmp/rustup-init
ENV PATH=/root/.cargo/bin:$PATH
# CPython 3.11.18 and 3.13.18 for the py311 and py313 lanes, beside Ubuntu's own 3.12. uv 0.13.7,
# checked against its published SHA-256, installs them, checking each download against the
# checksum it carries.
RUN true \
    && case "$(dpkg --print-architecture)" in \
      amd64) triple=x86_64-unknown-linux-gnu \
        sum=c5221ff41b7892bfc223543883600125f6a47fe845093e0f5bdff4f359196d83 ;; \
      arm64) triple=aarch64-unknown-linux-gnu \
        sum=71279f8d3c67241e807b9d8db872d1bf541579fb9f08b7d1eb1682196d1e57dc ;; \
      *) echo "no pinned uv for $(dpkg --print-architecture)" >&2; exit 1 ;; \
    esac \
    && curl -sSfL -o /tmp/uv.tar.gz \
      "https://github.com/astral-sh/uv/releases/download/0.13.7/uv-$triple.tar.gz" \
    && echo "$sum  /tmp/uv.tar.gz" | sha256sum -c - \
    && mkdir /tmp/uv && tar -xzf /tmp/uv.tar.gz -C /tmp/uv --strip-components=1 \
    && export UV_PYTHON_INSTALL_DIR=/opt/python UV_PYTHON_PREFERENCE=only-managed \
    && /tmp/uv/uv python install 3.11.18 3.13.18 \
    && ln -s "$(/tmp/uv/uv python find 3.11.18)" /usr/local/bin/python3.11 \
    && ln -s "$(/tmp/uv/uv python find 3.13.18)" /usr/local/bin/python3.13 \
    && rm -rf /tmp/uv /tmp/uv.tar.gz \
    && python3.11 -c 'import ssl, venv, sys; assert sys.version_info[:3] == (3, 11, 18)' \
    && python3.13 -c 'import ssl, venv, sys; assert sys.version_info[:3] == (3, 13, 18)'
