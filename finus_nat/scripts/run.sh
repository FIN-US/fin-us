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

# Mem0 등 NAT 전용 비밀: finus_nat/.env 를 마지막에 로드해 fin-us/.env 보다 우선한다.
if [[ -f "${FE_PKG_ABS}/.env" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "${FE_PKG_ABS}/.env"
  set +a
fi

# Self-hosted Mem0 compatibility for local `run.sh` execution:
# mem0 OSS endpoints may not implement cloud `/v1/ping` validation.
if [[ -n "${FINUS_MEM0_HOST:-}" || -n "${MEM0_API_KEY:-}" ]]; then
  uv run --project "${FE_PKG}" python - <<'PY'
from pathlib import Path

target = Path("finus_nat/.venv/lib/python3.12/site-packages/nat/plugins/mem0ai/memory.py")
if not target.exists():
    raise SystemExit(0)

text = target.read_text()
old = """    mem0_api_key = os.environ.get("MEM0_API_KEY")\n\n    if mem0_api_key is None:\n        raise RuntimeError("Mem0 API key is not set. Please specify it in the environment variable 'MEM0_API_KEY'.")\n\n    mem0_client = AsyncMemoryClient(api_key=mem0_api_key,\n                                    host=config.host,\n                                    org_id=config.org_id,\n                                    project_id=config.project_id)\n"""
new = """    mem0_api_key = os.environ.get("MEM0_API_KEY")\n\n    if mem0_api_key is None:\n        if config.host:\n            mem0_api_key = "selfhost-mem0-static-key"\n        else:\n            raise RuntimeError("Mem0 API key is not set. Please specify it in the environment variable 'MEM0_API_KEY'.")\n\n    if config.host:\n        original_validate = AsyncMemoryClient._validate_api_key\n\n        def _skip_validate(self):\n            return "selfhost-user"\n\n        AsyncMemoryClient._validate_api_key = _skip_validate\n        try:\n            mem0_client = AsyncMemoryClient(api_key=mem0_api_key,\n                                            host=config.host,\n                                            org_id=config.org_id,\n                                            project_id=config.project_id)\n        finally:\n            AsyncMemoryClient._validate_api_key = original_validate\n\n        mem0_client.async_client.headers.pop("Authorization", None)\n        mem0_client.async_client.headers.pop("Mem0-User-ID", None)\n    else:\n        mem0_client = AsyncMemoryClient(api_key=mem0_api_key,\n                                        host=config.host,\n                                        org_id=config.org_id,\n                                        project_id=config.project_id)\n"""

if old in text and new not in text:
    target.write_text(text.replace(old, new, 1))

client_target = Path("finus_nat/.venv/lib/python3.12/site-packages/mem0/client/main.py")
if client_target.exists():
    client_text = client_target.read_text()
    client_old = """        response = await self.async_client.post("/v1/memories/", json=payload)\n"""
    client_new = """        endpoint = "/memories" if self.host and "api.mem0.ai" not in self.host else "/v1/memories/"\n        response = await self.async_client.post(endpoint, json=payload)\n"""
    if client_old in client_text and client_new not in client_text:
        client_text = client_text.replace(client_old, client_new, 1)

    client_old = """        response = await self.async_client.post(f"/{version}/memories/search/", json=payload)\n"""
    client_new = """        endpoint = "/search" if self.host and "api.mem0.ai" not in self.host else f"/{version}/memories/search/"\n        response = await self.async_client.post(endpoint, json=payload)\n"""
    if client_old in client_text and client_new not in client_text:
        client_text = client_text.replace(client_old, client_new, 1)

    client_target.write_text(client_text)

editor_target = Path("finus_nat/.venv/lib/python3.12/site-packages/nat/plugins/mem0ai/mem0_editor.py")
if editor_target.exists():
    editor_text = editor_target.read_text()
    editor_old = """    async def add_items(self, items: list[MemoryItem]) -> None:\n"""
    editor_new = """    async def add_items(self, items: list[MemoryItem], **kwargs) -> None:\n"""
    if editor_old in editor_text and editor_new not in editor_text:
        editor_text = editor_text.replace(editor_old, editor_new, 1)

    editor_old = """                                 metadata=item_meta,\n                                 output_format="v1.1"))\n"""
    editor_new = """                                 metadata=item_meta,\n                                 output_format="v1.1",\n                                 **kwargs))\n"""
    if editor_old in editor_text and editor_new not in editor_text:
        editor_text = editor_text.replace(editor_old, editor_new, 1)

    editor_old = """        user_id = kwargs.pop("user_id")  # Ensure user ID is in keyword arguments\n\n        search_result = await self._client.search(query, user_id=user_id, top_k=top_k, output_format="v1.1", **kwargs)\n"""
    editor_new = """        user_id = kwargs.pop("user_id")  # Ensure user ID is in keyword arguments\n        search_kwargs = dict(kwargs)\n\n        host = getattr(self._client, "host", "") or ""\n        if "api.mem0.ai" in host:\n            search_kwargs["user_id"] = user_id\n        else:\n            search_kwargs["filters"] = {"user_id": user_id}\n\n        try:\n            search_result = await self._client.search(query, top_k=top_k, output_format="v1.1", **search_kwargs)\n        except Exception:\n            if "api.mem0.ai" in host:\n                raise\n\n            response = await self._client.async_client.get("/memories", params={"user_id": user_id})\n            response.raise_for_status()\n            results = response.json().get("results", [])\n            needle = query.casefold().strip()\n            if needle:\n                matched = [item for item in results if needle in str(item.get("memory", "")).casefold()]\n                if matched:\n                    results = matched\n            search_result = {"results": results[:top_k]}\n"""
    if editor_old in editor_text and editor_new not in editor_text:
        editor_text = editor_text.replace(editor_old, editor_new, 1)
        editor_target.write_text(editor_text)

agent_target = Path("finus_nat/.venv/lib/python3.12/site-packages/nat/plugins/langchain/agent/auto_memory_wrapper/agent.py")
if agent_target.exists():
    agent_text = agent_target.read_text()
    agent_old = """        user_manager = self._context.user_manager\n"""
    agent_new = """        user_manager = getattr(self._context, "user_manager", None)\n"""
    if agent_old in agent_text and agent_new not in agent_text:
        agent_text = agent_text.replace(agent_old, agent_new, 1)

    agent_old = """        if self._context.metadata and self._context.metadata.headers:\n            user_id = self._context.metadata.headers.get("x-user-id")\n"""
    agent_new = """        metadata = getattr(self._context, "metadata", None)\n        headers = getattr(metadata, "headers", None)\n        if headers:\n            user_id = headers.get("x-user-id")\n"""
    if agent_old in agent_text and agent_new not in agent_text:
        agent_text = agent_text.replace(agent_old, agent_new, 1)

    agent_target.write_text(agent_text)
PY
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
