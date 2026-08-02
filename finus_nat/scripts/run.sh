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

# Self-hosted Mem0 compatibility gate: mem0 OSS 엔드포인트는 클라우드 전용
# /v1/ping 검증이 없어서, 벤더 패치(scripts/patch_vendor.py)가 안 붙으면 Mem0
# 경로가 깨진다. 패치 정의는 Dockerfile과 공유하는 scripts/patch_vendor.py
# 하나에만 둔다.
#
# 이 게이트는 파싱이 끝나 최종값이 된 _CONFIG_FILE(이 실행이 실제로 무엇을
# 쓰는지)로 판단한다. 예전에는 env(FINUS_MEM0_HOST/MEM0_API_KEY) 존재만 보고
# 판단했는데, 공용 .env가 항상 MEM0_API_KEY를 갖고 있어 Mem0를 전혀 쓰지 않는
# 기본 --nomemory 실행까지 벤더 drift 경고에 걸렸다.
#
# 이전 코멘트는 "여기서는 실패해도 런처를 죽이지 않는다 … 강제 지점은 Docker
# 빌드(Dockerfile)이고, 로컬은 경고로 충분하다"였다. 이제는 뒤집는다: 이 실행이
# 실제로 mem0_memory를 쓰는 config를 골랐는데 패치가 안 붙었다면, 조용히
# 넘어가는 것은 사용자에게 원인 불명의 런타임 실패만 남긴다. 게이트가 env
# 스코프에서 config 스코프로 좁혀졌기 때문에 이제는 로컬에서도 fatal로
# 처리해도 무관하다 - --nomemory 기본 실행은 애초에 이 블록에 들어오지 않는다.
#
# NAT config는 `base:` 상속(재귀 deep-merge, nat/utils/io/yaml_tools.py)을 쓴다.
# router.yml -> agents/diary_agent.yml -> ... -> ../common.yml처럼 체인이 길게
# 이어질 수 있어서, _CONFIG_FILE 자체(leaf)에는 mem0_memory가 한 번도 안 나와도
# base 체인을 타고 들어가면 Mem0 워크플로로 귀결될 수 있다. leaf만 grep하면 이
# 경우를 조용히 놓친다 - 그게 issue #65가 없애려는 바로 그 silent-skip 부류다.
#
# 이 체인 해석은 셸에서 직접 흉내내지 않는다. 예전에는 grep/sed로 `base:` 줄을
# 파싱해 따라갔는데, yaml_tools.py의 실제 로더와 두 지점에서 갈라졌다: (1) 절대
# 경로 base(`base: /abs/path.yml`)를 상대 경로처럼 현재 디렉터리에 이어붙여
# "파일을 찾을 수 없다"로 조용히 skip했고, (2) `base: deep.yml  # comment`처럼
# 인라인 주석이 붙으면 sed가 그 주석까지 파일명에 포함시켜 역시 조용히 skip했다.
# 둘 다 게이트를 무력화하는 결과(GATE=SKIP)라 issue #65가 없애려던 바로 그
# silent-skip이 가드 안에서 재현되는 셈이었다. 셸로 nat 로더를 재구현하며 계속
# 따라잡는 대신, nat.utils.io.yaml_tools.yaml_load()를 그대로 호출해 병합된
# dict를 검사한다 - 상속 규칙이 갈라질 여지 자체가 없다. 인터프리터는 두 줄
# 뒤 patch_vendor.py 호출에서 어차피 필요하므로 추가 의존성도 아니다.
#
# 종료 코드: 0=mem0 사용, 3=mem0 미사용, 2=판단 불가(설정 파일 없음/파싱 실패/
# 순환 참조 등). 2는 "쓰지 않는다"고 조용히 넘어가면 안 되는 경우이므로 호출부가
# fail-fast로 처리한다 - 존재하지 않는 설정 파일에 대해 "찾을 수 없습니다" 경고를
# 찍고 나서 바로 "mem0_memory를 쓰지 않습니다"라고 단정하는 것은 이 스크립트가
# 방금 확인 불가하다고 말한 사실을 스스로 뒤집는 것이었다.
#
# "미사용"에 1이 아니라 3을 쓰는 이유: 1은 `uv run` 자체가 실패했을 때도 나오는
# 종료 코드다(예: 오프라인 상태의 콜드 캐시에서 `litellm==1.83.0` 다운로드 실패,
# 또는 -c 스크립트 안의 미처리 예외). 예전에는 그 1이 그대로 "mem0 미사용"으로
# 해석돼, 네트워크 문제나 리졸버 실패가 "이 설정은 mem0를 안 쓴다"는 확신에 찬
# 오판으로 둔갑했다 - issue #65가 없애려던 바로 그 silent-skip 부류다. 그래서
# "미사용"은 이 스크립트가 명시적으로 고르는 흔치 않은 코드(3)로 옮기고, 0/2/3
# 이외의 모든 값(1 포함)은 게이트 자체가 실패한 것으로 간주해 무조건 fatal
# 처리한다. `os.environ["CONFIG_FILE_FOR_PY"]`의 KeyError나 `_contains_mem0`
# 안의 RecursionError 같은 미처리 예외도 이제 이 catch-all(2)로 떨어진다.
_config_uses_mem0() {
  local file="${1}"
  CONFIG_FILE_FOR_PY="${file}" uv run --project "${FE_PKG}" python -c '
import os
import sys

try:
    from nat.utils.io.yaml_tools import yaml_load
except Exception as exc:
    print(f"경고: nat.utils.io.yaml_tools를 불러오지 못했습니다: {exc}", file=sys.stderr)
    sys.exit(2)


def _contains_mem0(value) -> bool:
    if isinstance(value, dict):
        return any(_contains_mem0(v) for v in value.values())
    if isinstance(value, list):
        return any(_contains_mem0(v) for v in value)
    return isinstance(value, str) and "mem0_memory" in value


try:
    path = os.environ["CONFIG_FILE_FOR_PY"]
    config = yaml_load(path)
    uses_mem0 = _contains_mem0(config)
except Exception as exc:
    print(f"경고: mem0 사용 여부를 판단하지 못했습니다({exc.__class__.__name__}): {exc}", file=sys.stderr)
    sys.exit(2)

sys.exit(0 if uses_mem0 else 3)
'
}

_mem0_rc=0
_config_uses_mem0 "${_CONFIG_FILE}" || _mem0_rc=$?
case "${_mem0_rc}" in
  0)
    if ! uv run --project "${FE_PKG}" python "${FE_PKG}/scripts/patch_vendor.py" \
      --venv "${FE_PKG_ABS}/.venv"; then
      echo "오류: 벤더 패치를 적용하지 못했습니다. ${_CONFIG_FILE}이(가) mem0_memory를 쓰는데 패치가 없으면 Mem0 경로가 런타임에 원인 불명으로 실패합니다." >&2
      echo "      NAT/mem0 버전을 올렸다면 ${FE_PKG}/scripts/patch_vendor.py 의 원본 문자열을 갱신하세요." >&2
      exit 1
    fi
    ;;
  3)
    echo "벤더 패치 건너뜀: ${_CONFIG_FILE}이(가) mem0_memory를 쓰지 않습니다." >&2
    ;;
  2)
    echo "오류: ${_CONFIG_FILE}의 mem0 사용 여부를 판단할 수 없습니다(위 경고 참고). 원인 불명 실패로 이어지느니 여기서 멈춘다." >&2
    exit 1
    ;;
  *)
    echo "오류: mem0 게이트(_config_uses_mem0)가 예상 밖 종료 코드(${_mem0_rc})로 끝났습니다. uv run 자체가 실패했을 수 있습니다(오프라인/의존성 다운로드 실패 등). 원인 불명 실패로 이어지느니 여기서 멈춘다." >&2
    exit 1
    ;;
esac

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
