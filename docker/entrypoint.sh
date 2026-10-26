#!/bin/sh
set -eu

PUID="${PUID:-568}"
PGID="${PGID:-568}"
PORT="${PORT:-8095}"

mkdir -p /config
chown -R "${PUID}:${PGID}" /config

exec gosu "${PUID}:${PGID}" \
  python -m uvicorn server.main:app \
    --host 0.0.0.0 \
    --port "${PORT}" \
    --proxy-headers \
    --forwarded-allow-ips='*' \
    --no-access-log
