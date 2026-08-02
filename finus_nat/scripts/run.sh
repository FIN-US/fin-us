#!/usr/bin/env bash
# finus_nat/scripts/run.sh 으로 실행하면 지속적으로 대화할 수 있습니다.
# --memory 옵션을 사용하면 finus_nat/configs/router.yml을 사용합니다.
# --nomemory 옵션을 사용하면 finus_nat/configs/router_nomemory.yml을 사용합니다. 기본적으로 finus_nat/configs/router_nomemory.yml을 사용합니다.
# --once 옵션을 사용하면 한번 실행하고 워크플로우를 종료합니다.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "${ROOT}"

FE_PKG="finus_nat"

FE_PKG_ABS="${ROOT}/${FE_PKG}"

_nat_sh="${FE_PKG_ABS}/.venv/bin/nat"
if [[ -f "${_nat_sh}" ]]; then
  _py_line="$(head -1 "${_nat_sh}" || true)"
  _py="${_py_line/#\#!/}"
  if [[ -n "${_py}" && ! -x "${_py}" ]]; then
    echo "Removing stale ${FE_PKG}/.venv (nat launcher pointed at missing Python)." >&2
    rm -rf "${FE_PKG_ABS}/.venv"
  fi
fi

_default_uv_cache="${HOME}/.cache/uv"
if [[ -z "${UV_CACHE_DIR:-}" ]] && [[ -e "${_default_uv_cache}" ]] && [[ ! -w "${_default_uv_cache}" ]]; then
  export UV_CACHE_DIR="${HOME}/.cache/uv-${USER}"
fi

# Optional local secrets (fin-us/backend/.env is used by the Fin-Us backend stack)
for _env in "${ROOT}/backend/.env" "${ROOT}/.env"; do
  if [[ -f "${_env}" ]]; then
    set -a
    # shellcheck disable=SC1090
    source "${_env}"
    set +a
  fi
done

# common.yml reads OPENAI_API_BASE_URL; backend .env often sets OPENAI_BASE_URL (OpenRouter, etc.)
if [[ -z "${OPENAI_API_BASE_URL:-}" && -n "${OPENAI_BASE_URL:-}" ]]; then
  export OPENAI_API_BASE_URL="${OPENAI_BASE_URL}"
fi

# Mem0 등 NAT 전용 비밀(MEM0_API_KEY, FINUS_MEM0_*, FINUS_KIS_TRADING_MCP_URL,
# FINUS_BACKEND_URL)도 fin-us/.env 로 통합되었습니다. 별도 finus_nat/.env 는 더 이상 사용하지 않습니다.

# Self-hosted Mem0 compatibility for local `run.sh` execution:
# mem0 OSS endpoints may not implement cloud `/v1/ping` validation.
# 패치 정의는 Dockerfile과 공유하는 scripts/patch_vendor.py 하나에만 둔다.
#
# 여기서는 실패해도 런처를 죽이지 않는다. 이 게이트는 위에서 source한 공용 .env의
# MEM0_API_KEY만 있어도 통과하므로, Mem0를 쓰지 않는 기본 --nomemory 실행까지
# 벤더 drift 하나로 막히게 된다. 강제 지점은 Docker 빌드(Dockerfile)이고,
# 로컬은 경고로 충분하다. set -e에 걸리지 않도록 || 로 받는다.
if [[ -n "${FINUS_MEM0_HOST:-}" || -n "${MEM0_API_KEY:-}" ]]; then
  uv run --project "${FE_PKG}" python "${FE_PKG}/scripts/patch_vendor.py" \
    --venv "${FE_PKG_ABS}/.venv" || {
    echo "경고: 벤더 패치를 적용하지 못했습니다. Mem0(--memory) 경로가 정상 동작하지 않을 수 있습니다." >&2
    echo "      NAT/mem0 버전을 올렸다면 ${FE_PKG}/scripts/patch_vendor.py 의 원본 문자열을 갱신하세요." >&2
  }
fi

_default_config="${FE_PKG}/configs/router_nomemory.yml"
_CONFIG_FILE="${FINUS_NAT_CONFIG_FILE:-${_default_config}}"
_CHAT_PORT="${FINUS_CHAT_PORT:-8765}"
_CHAT_URL="${FINUS_NAT_URL:-}"

# Multi-turn chat: NAT OpenAI-compatible API + ``finus_sqlite_transcript_agent`` (SQLite).
#   REPL: ``finus-chat`` sends ``conversation-id`` (see ``nat_finus_nat.transcript``); local ``messages`` + server transcript.
#   FINUS_CHAT_CONVERSATION_ID=<uuid>  재실행 시 같은 DB 스레드 유지 (미설정 시 매 실행 새 UUID)
#   FINUS_CHAT_COT=0  단계 출력 끔  |  FINUS_CHAT_STREAM=0  비스트림 한 방 응답
#   FINUS_CHAT_COLOR=0  stderr ANSI 끔
#   ./scripts/run.sh chat              # 로컬에서 nat serve 띄운 뒤 REPL
#   ./scripts/run.sh -chat             # 위와 동일(오타/관용 별칭)
#   ./scripts/run.sh chat 첫 질문     # 첫 줄 입력 생략
#   FINUS_NAT_URL=http://127.0.0.1:8000 ./scripts/run.sh chat   # 이미 떠 있는 서버에만 붙기
# Default mode: chat REPL
_run_mode="chat"
while [[ $# -gt 0 ]]; do
  case "${1}" in
    --memory)
      _CONFIG_FILE="${FE_PKG}/configs/router.yml"
      shift
      ;;
    --nomemory)
      _CONFIG_FILE="${FE_PKG}/configs/router_nomemory.yml"
      shift
      ;;
    --once)
      _run_mode="once"
      shift
      ;;
    --help|-h)
      cat <<EOF
Usage: ./scripts/run.sh [--memory|--nomemory] [--once] [chat|-chat|-i|--interactive] [initial_message]

Options:
  --memory     Use Mem0-enabled router config (${FE_PKG}/configs/router.yml)
  --nomemory   Use no-memory router config (${FE_PKG}/configs/router_nomemory.yml, default)
  --once       Run single-turn mode (nat run --input ...) and exit
  -h, --help   Show this help message

Env override:
  FINUS_NAT_CONFIG_FILE=<path> is applied first, then CLI option can override it.
EOF
      exit 0
      ;;
    chat|-chat|-i|--interactive)
      _run_mode="chat"
      shift
      break
      ;;
    *)
      break
      ;;
  esac
done

if [[ "${_run_mode}" == "chat" ]]; then
  _own_serve=0
  _serve_pid=""
  if [[ -z "${_CHAT_URL}" ]]; then
    _CHAT_URL="http://127.0.0.1:${_CHAT_PORT}"
    _own_serve=1
    echo "Starting NAT (fastapi) at ${_CHAT_URL} …" >&2
    uv run --project "${FE_PKG}" nat serve --config_file "${_CONFIG_FILE}" --host 127.0.0.1 --port "${_CHAT_PORT}" &
    _serve_pid=$!
    _deadline=$((SECONDS + 90))
    until curl -sf "${_CHAT_URL}/health" >/dev/null 2>&1; do
      if ! kill -0 "${_serve_pid}" 2>/dev/null; then
        echo "NAT serve process exited before /health became ready." >&2
        exit 1
      fi
      if [[ "${SECONDS}" -ge "${_deadline}" ]]; then
        echo "Timed out waiting for ${_CHAT_URL}/health" >&2
        kill "${_serve_pid}" 2>/dev/null || true
        exit 1
      fi
      sleep 0.4
    done
  fi

  _cleanup_chat() {
    if [[ "${_own_serve}" == "1" && -n "${_serve_pid}" ]]; then
      kill "${_serve_pid}" 2>/dev/null || true
      wait "${_serve_pid}" 2>/dev/null || true
    fi
  }
  trap _cleanup_chat EXIT INT TERM

  # Restore canonical terminal editing (backspace/delete) before entering REPL.
  if [[ -t 0 ]]; then
    stty sane 2>/dev/null || true
  fi

  export FINUS_NAT_URL="${_CHAT_URL}"
  export FINUS_CHAT_INITIAL="${*:-}"
  PYTHONUTF8=1 PYTHONIOENCODING=utf-8:replace PYTHONUNBUFFERED=1 uv run --project "${FE_PKG}" finus-chat
  exit 0
fi

uv run --project "${FE_PKG}" nat run --config_file "${_CONFIG_FILE}" --input "${1:-Hello}"
