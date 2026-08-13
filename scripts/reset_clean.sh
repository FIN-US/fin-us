#!/usr/bin/env bash

set -euo pipefail

_auto=""
for _a in "$@"; do
  case "${_a}" in
    -y | --yes) _auto=1 ;;
  esac
done

source "$(dirname "${BASH_SOURCE[0]}")/_env.sh"
ROOT="${FIN_US_INTEGRATE_ROOT}"

if [[ "${_auto}" != "1" ]]; then
  read -r -p "Run 'docker compose down -v --rmi local' and delete local node_modules/venv/__pycache__ under ${ROOT}? [y/N] " _ans || true
  case "${_ans}" in
    y | Y | yes | YES) ;;
    *)
      echo "Aborted." >&2
      exit 1
      ;;
  esac
fi

echo "== Docker Compose (stack, volumes, images built for this compose file) =="
if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
  cd "${ROOT}"
  docker compose down -v --rmi local --remove-orphans || true
else
  echo "  (docker or docker compose not available; skipped)"
fi

echo "== Local directories =="
_rm_if_exists() {
  local p="$1"
  if [[ -e "${p}" ]]; then
    rm -rf "${p}"
    echo "  removed ${p}"
  fi
}

_rm_if_exists "${ROOT}/mcp-news/node_modules"
_rm_if_exists "${ROOT}/mcp-trading/node_modules"
_rm_if_exists "${ROOT}/backend/venv"
_rm_if_exists "${ROOT}/finus_nat/.venv"
_rm_if_exists "${ROOT}/finus_nat/.pytest_cache"
_rm_if_exists "${ROOT}/finus_nat/build"
_rm_if_exists "${ROOT}/finus_nat/dist"
_rm_if_exists "${ROOT}/.pytest_cache"

while IFS= read -r -d '' _d; do
  rm -rf "${_d}"
  echo "  removed ${_d}"
done < <(find "${ROOT}/backend" "${ROOT}/finus_nat" -type d -name __pycache__ -print0 2>/dev/null || true)

while IFS= read -r -d '' _d; do
  rm -rf "${_d}"
  echo "  removed ${_d}"
done < <(find "${ROOT}/finus_nat" -type d -name "*.egg-info" -print0 2>/dev/null || true)

echo
echo "OK. Rebuild / install when ready:"
echo "  bash ${ROOT}/scripts/setup_deps.sh"
echo "  bash ${ROOT}/scripts/run_stack.sh"
