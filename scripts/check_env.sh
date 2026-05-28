#!/usr/bin/env bash

set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/_env.sh"

ok=0
warn=0

die_msg() { echo "  ✗ $*" >&2; ok=1; }

_env_nonempty_key() {
  local f="$1" key="$2"
  [[ -f "${f}" ]] || return 1
  grep -E "^[[:space:]]*${key}[[:space:]]*=[[:space:]]*[^[:space:]]" "${f}" >/dev/null 2>&1
}

echo "== Fin-Us integrated stack (Docker) =="
echo "FIN_US_INTEGRATE_ROOT=${FIN_US_INTEGRATE_ROOT}"
echo

if command -v docker >/dev/null 2>&1; then
  echo "  ✓ docker ($(command -v docker))"
else
  die_msg "docker not found"
fi

if docker compose version >/dev/null 2>&1; then
  echo "  ✓ docker compose $(docker compose version --short 2>/dev/null || echo ok)"
else
  die_msg "docker compose not available"
fi

if [[ ! -d "${FINUS_NAT_DIR}" ]]; then
  die_msg "missing ${FINUS_NAT_DIR}"
else
  echo "  ✓ finus_nat present"
fi

if [[ ! -d "${FIN_BACKEND}" ]]; then
  die_msg "missing ${FIN_BACKEND}"
else
  echo "  ✓ backend present"
fi

_env_backend="${FIN_US_INTEGRATE_ROOT}/.env"
if [[ -f "${_env_backend}" ]]; then
  echo "  ✓ root .env present (${_env_backend})"
else
  echo "  ! root .env missing — copy .env.example to .env" >&2
  warn=1
fi

if [[ -f "${_env_backend}" ]]; then
  if _env_nonempty_key "${_env_backend}" "OPENAI_API_KEY"; then
    echo "  ✓ OPENAI_API_KEY is set (default analyze + NAT agents)"
  elif _env_nonempty_key "${_env_backend}" "ANTHROPIC_API_KEY"; then
    echo "  · OPENAI_API_KEY not set — OK if you only use Anthropic in the UI; NAT multi-agent still needs OpenAI key"
  else
    echo "  ! OPENAI_API_KEY and ANTHROPIC_API_KEY both missing — set at least one for analyze routes" >&2
    warn=1
  fi
  if _env_nonempty_key "${_env_backend}" "ANTHROPIC_API_KEY"; then
    echo "  ✓ ANTHROPIC_API_KEY is set"
  else
    echo "  · ANTHROPIC_API_KEY not set (optional; pick Anthropic in UI when set)"
  fi
fi

if [[ ! -f "${FIN_US_INTEGRATE_ROOT}/docker-compose.yml" ]]; then
  die_msg "missing docker-compose.yml under ${FIN_US_INTEGRATE_ROOT}"
else
  echo "  ✓ docker-compose.yml present"
fi

echo "  · Ollama: optional. NAT defaults to OpenAI; for local Ollama see finus_nat/configs/common.yml (ollama_llm)."
if command -v ollama >/dev/null 2>&1; then
  echo "    - ollama CLI present"
fi
if command -v curl >/dev/null 2>&1 && [[ -n "${OLLAMA_OPENAI_BASE_URL:-}" ]]; then
  _base="${OLLAMA_OPENAI_BASE_URL%/}"
  if curl -sf --max-time 2 "${_base}/models" >/dev/null 2>&1; then
    echo "    - Ollama-compatible endpoint reachable at ${_base}/models"
  fi
fi

echo
if [[ "${ok}" -ne 0 ]]; then
  echo "Fix errors above."
  exit 1
fi
if [[ "${warn}" -ne 0 ]]; then
  echo "Warnings only — set OPENAI_API_KEY (and optional keys) in .env before using analyze/NAT."
fi
echo "OK."
