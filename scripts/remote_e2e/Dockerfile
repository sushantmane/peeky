# A separate Linux host for the remote-worker end-to-end test (#8): sshd with key-only
# login, Peeky installed, and a copy of the example programs. Build from the repo root:
#   docker build -f scripts/remote_e2e/Dockerfile --target host -t peeky-remote-e2e .
# The `java` target adds a JDK and the Java adapter bundle (#248):
#   docker build -f scripts/remote_e2e/Dockerfile --target java -t peeky-remote-e2e-java .
# The `native` target adds C, C++ and Rust (#252):
#   docker build -f scripts/remote_e2e/Dockerfile --target native -t peeky-remote-e2e-native .
FROM eclipse-temurin:17-jdk@sha256:fe947d993c32dd9cf651a4353a6ed723c2370b3a9d034ea4cab4775404ea531a AS jdk

FROM python:3.11-slim@sha256:e88e9763f943ec1834f992a4b51e0f24500486803e8bc534e5767af9ea65f6ce AS base
RUN apt-get update \
    && apt-get install -y --no-install-recommends openssh-server \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --shell /bin/sh peeky \
    && mkdir -p /run/sshd
COPY scripts/remote_e2e/sshd_config /etc/ssh/sshd_config.d/peeky.conf
COPY scripts/remote_e2e/entrypoint.sh /usr/local/bin/peeky-e2e-entrypoint
EXPOSE 22
ENTRYPOINT ["/usr/local/bin/peeky-e2e-entrypoint"]

FROM base AS host
COPY pyproject.toml README.md MANIFEST.in /opt/peeky/
COPY src /opt/peeky/src
RUN python -m pip install --no-cache-dir --disable-pip-version-check /opt/peeky
COPY --chown=peeky:peeky examples/pagination_bug.py examples/verify_pagination.py /home/peeky/app/

# Temurin 17, as the hosted Java lane uses. The links put java and javac on the PATH of
# ssh sessions, which don't see Docker's ENV.
FROM host AS java
COPY --from=jdk /opt/java/openjdk /opt/java/openjdk
RUN ln -s /opt/java/openjdk/bin/java /opt/java/openjdk/bin/javac /usr/local/bin/
COPY scripts/setup_java_adapter.py /opt/peeky/scripts/
COPY adapters/java /opt/peeky/adapters/java
RUN python /opt/peeky/scripts/setup_java_adapter.py --output /opt/peeky-java >/dev/null
COPY --chown=peeky:peeky examples/java/demo/PaginationBug.java /home/peeky/java/demo/

# C, C++ and Rust: LLVM 18 (clang, lldb-dap, llvm-dwarfdump) from Debian 13's signed
# packages, and Rust 1.96.0: the majors and the toolchain the CI lanes use (scripts/ci_linux).
# rustup-init 1.29.2 is checked against its published SHA-256 for the architecture before
# it runs. It installs into /opt/rust, readable by the ssh user; tools.json names the
# toolchain's real cargo and rustc, which Rust configs need. Installed before Peeky, so a
# change to Peeky's source doesn't rebuild the toolchain.
FROM base AS native
ENV RUSTUP_HOME=/opt/rust/rustup CARGO_HOME=/opt/rust/cargo
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
      clang-18 lldb-18 llvm-18 libc6-dev libstdc++-14-dev curl ca-certificates \
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
    && /tmp/rustup-init -y --profile minimal --default-toolchain 1.96.0 --no-modify-path \
    && rm /tmp/rustup-init \
    && printf '{"cargo": "%s", "rustc": "%s"}\n' \
      "$(/opt/rust/cargo/bin/rustup which --toolchain 1.96.0 cargo)" \
      "$(/opt/rust/cargo/bin/rustup which --toolchain 1.96.0 rustc)" > /opt/rust/tools.json \
    && chmod -R a+rX /opt/rust \
    && apt-get purge -y curl && apt-get autoremove -y && rm -rf /var/lib/apt/lists/* \
    && test -x /usr/bin/lldb-dap-18
COPY pyproject.toml README.md MANIFEST.in /opt/peeky/
COPY src /opt/peeky/src
RUN python -m pip install --no-cache-dir --disable-pip-version-check /opt/peeky
