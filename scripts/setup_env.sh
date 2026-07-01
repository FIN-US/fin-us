#!/usr/bin/env bash

set -euo pipefail
_SCRIPTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${_SCRIPTS_DIR}/_env.sh"

cd "${FIN_US_INTEGRATE_ROOT}"

UV_CACHE_DIR="${UV_CACHE_DIR:-.uv-cache}" uv run --project backend python backend/scripts/setup_env.py "$@"
