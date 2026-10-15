#!/usr/bin/env bash

set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/_env.sh"

command -v docker >/dev/null || { echo "ERROR: docker required" >&2; exit 1; }

if command -v npm >/dev/null 2>&1; then
  echo "== MCP Node deps (mcp/news, mcp/trading, mcp/dart) =="
  bash "${FIN_US_INTEGRATE_ROOT}/scripts/install_fin_us_mcp.sh"
else
  echo "WARN: npm not found. Install Node.js and run: bash scripts/install_fin_us_mcp.sh" >&2
  echo "      (backend needs mcp/*/node_modules on the host bind mount.)" >&2
fi

echo "== docker compose build (finus-nat, backend, frontend) =="
cd "${FIN_US_INTEGRATE_ROOT}"
docker compose build

echo
echo "OK. Start the stack:"
echo "  bash ${FIN_US_INTEGRATE_ROOT}/scripts/run_stack.sh"
echo "Or one service: bash ${FIN_US_INTEGRATE_ROOT}/scripts/run_stack.sh backend"
