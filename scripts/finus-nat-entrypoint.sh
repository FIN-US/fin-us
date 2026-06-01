#!/bin/sh
set -e

for pkg in mcp-news mcp-trading mcp-dart; do
  modules="/workspace/${pkg}/node_modules"
  if [ ! -d "${modules}" ] || [ -z "$(ls -A "${modules}" 2>/dev/null)" ]; then
    echo "finus-nat: installing ${pkg} npm dependencies..."
    (cd "/workspace/${pkg}" && npm ci --omit=dev)
  fi
done

exec "$@"
