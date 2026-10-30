#!/bin/sh
# Keep a SOCKS5 tunnel up forever. Key: /keys/id_ed25519 (Secret muse2api-egress-ssh).
# Host key pinned via /keys/known_hosts so a swapped server is refused, not trusted.
set -u
: "${EGRESS_HOST:?}" "${EGRESS_USER:?}"
PORT="${SOCKS_PORT:-1080}"
cp /keys/id_ed25519 /tmp/k && chmod 600 /tmp/k
while :; do
  echo "$(date -u +%FT%TZ) tunnel up -> ${EGRESS_USER}@${EGRESS_HOST} socks 127.0.0.1:${PORT}"
  ssh -N -i /tmp/k -D "127.0.0.1:${PORT}" \
      -o BatchMode=yes -o ExitOnForwardFailure=yes \
      -o StrictHostKeyChecking=yes -o UserKnownHostsFile=/keys/known_hosts \
      -o ServerAliveInterval=15 -o ServerAliveCountMax=3 -o TCPKeepAlive=yes \
      -o ConnectTimeout=15 "${EGRESS_USER}@${EGRESS_HOST}"
  echo "$(date -u +%FT%TZ) tunnel exited rc=$?, retry in 3s"
  sleep 3
done
