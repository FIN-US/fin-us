#!/usr/bin/env bash

set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/_env.sh"

if [[ ! -f "${FIN_US_INTEGRATE_ROOT}/.env" ]]; then
  echo "ERROR: root .env missing. Run: bash scripts/setup_env.sh" >&2
  exit 1
fi

command -v docker >/dev/null || { echo "ERROR: docker not found" >&2; exit 1; }

cd "${FIN_US_INTEGRATE_ROOT}"
exec docker compose up --build "$@"
