# A separate Linux host for the remote-worker end-to-end test (#8): sshd with key-only
# login, Peeky installed, and a copy of the example programs. Build from the repo root:
#   docker build -f scripts/remote_e2e/Dockerfile -t peeky-remote-e2e .
FROM python:3.11-slim@sha256:e88e9763f943ec1834f992a4b51e0f24500486803e8bc534e5767af9ea65f6ce
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
