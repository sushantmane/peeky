#!/bin/sh
# Installs the one authorized client key passed by the test, then runs sshd.
set -eu
: "${AUTHORIZED_KEY:?set AUTHORIZED_KEY to the client public key}"
install -d -m 700 -o peeky -g peeky /home/peeky/.ssh
printf '%s\n' "$AUTHORIZED_KEY" > /home/peeky/.ssh/authorized_keys
chown peeky:peeky /home/peeky/.ssh/authorized_keys
chmod 600 /home/peeky/.ssh/authorized_keys
ssh-keygen -A >/dev/null
exec /usr/sbin/sshd -D -e
