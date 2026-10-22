# A separate Linux host for the remote-worker end-to-end test (#8): sshd with key-only
# login, Peeky installed, and a copy of the example programs. Build from the repo root:
#   docker build -f scripts/remote_e2e/Dockerfile --target host -t peeky-remote-e2e .
# The `java` target adds a JDK and the Java adapter bundle (#248):
#   docker build -f scripts/remote_e2e/Dockerfile --target java -t peeky-remote-e2e-java .
FROM eclipse-temurin:17-jdk@sha256:fe947d993c32dd9cf651a4353a6ed723c2370b3a9d034ea4cab4775404ea531a AS jdk

FROM python:3.11-slim@sha256:e88e9763f943ec1834f992a4b51e0f24500486803e8bc534e5767af9ea65f6ce AS host
RUN apt-get update \
    && apt-get install -y --no-install-recommends openssh-server \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --shell /bin/sh peeky \
    && mkdir -p /run/sshd
COPY pyproject.toml README.md MANIFEST.in /opt/peeky/
COPY src /opt/peeky/src
RUN python -m pip install --no-cache-dir --disable-pip-version-check /opt/peeky
COPY --chown=peeky:peeky examples/pagination_bug.py examples/verify_pagination.py /home/peeky/app/
COPY scripts/remote_e2e/sshd_config /etc/ssh/sshd_config.d/peeky.conf
COPY scripts/remote_e2e/entrypoint.sh /usr/local/bin/peeky-e2e-entrypoint
EXPOSE 22
ENTRYPOINT ["/usr/local/bin/peeky-e2e-entrypoint"]

# Temurin 17, as the hosted Java lane uses. The links put java and javac on the PATH of
# ssh sessions, which don't see Docker's ENV.
FROM host AS java
COPY --from=jdk /opt/java/openjdk /opt/java/openjdk
RUN ln -s /opt/java/openjdk/bin/java /opt/java/openjdk/bin/javac /usr/local/bin/
COPY scripts/setup_java_adapter.py /opt/peeky/scripts/
COPY adapters/java /opt/peeky/adapters/java
RUN python /opt/peeky/scripts/setup_java_adapter.py --output /opt/peeky-java >/dev/null
COPY --chown=peeky:peeky examples/java/demo/PaginationBug.java /home/peeky/java/demo/
