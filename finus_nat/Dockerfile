FROM python:3.13-slim

# 파이프 도중 실패(curl 등)를 다음 명령으로 무시하지 않도록 bash pipefail 사용
SHELL ["/bin/bash", "-o", "pipefail", "-c"]

# DL3008: python:3.13-slim의 Debian 저장소는 패키지 구버전을 주기적으로 제거해 버전 핀은 오히려 재현성을 해친다.
# hadolint ignore=DL3008
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates gnupg \
    && curl -fsSL https://deb.nodesource.com/setup_24.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.9.28 /uv /uvx /bin/

WORKDIR /workspace

ARG SETUPTOOLS_SCM_PRETEND_VERSION=0.0.0+docker
ENV SETUPTOOLS_SCM_PRETEND_VERSION=${SETUPTOOLS_SCM_PRETEND_VERSION}

COPY finus_nat /workspace/finus_nat
COPY mcp-news /workspace/mcp-news
COPY mcp-trading /workspace/mcp-trading
COPY mcp-dart /workspace/mcp-dart

WORKDIR /workspace/mcp-news
RUN npm ci

WORKDIR /workspace/mcp-trading
RUN npm ci

WORKDIR /workspace/mcp-dart
RUN npm ci

WORKDIR /workspace/finus_nat
RUN uv sync --no-dev
# DL3059: uv sync와 mem0ai 패치는 캐시 무효화 시점이 다르므로 일부러 RUN을 분리한다.
# hadolint ignore=DL3059
RUN python - <<'PY'
from pathlib import Path

venv_lib = Path("/workspace/finus_nat/.venv/lib")
matches = list(venv_lib.glob("python3.*/site-packages/nat/plugins/mem0ai/memory.py"))
if len(matches) != 1:
    raise SystemExit(f"Expected one mem0ai memory.py, found {matches!r}")
target = matches[0]
text = target.read_text()
old = """    mem0_api_key = os.environ.get(\"MEM0_API_KEY\")\n\n    if mem0_api_key is None:\n        raise RuntimeError(\"Mem0 API key is not set. Please specify it in the environment variable 'MEM0_API_KEY'.\")\n"""
new = """    mem0_api_key = os.environ.get(\"MEM0_API_KEY\")\n\n    if mem0_api_key is None:\n        if config.host:\n            # Self-hosted Mem0: keep client init deterministic without env provisioning.\n            mem0_api_key = \"selfhost-mem0-static-key\"\n        else:\n            raise RuntimeError(\"Mem0 API key is not set. Please specify it in the environment variable 'MEM0_API_KEY'.\")\n"""
if old not in text:
    raise SystemExit("Expected Mem0 API key block not found")
text = text.replace(old, new, 1)

old = """    mem0_client = AsyncMemoryClient(api_key=mem0_api_key,\n                                    host=config.host,\n                                    org_id=config.org_id,\n                                    project_id=config.project_id)\n"""
new = """    if config.host:\n        # Open-source self-hosted Mem0 does not expose /v1/ping; bypass cloud-style API key validation.\n        original_validate = AsyncMemoryClient._validate_api_key\n\n        def _skip_validate(self):\n            return \"selfhost-user\"\n\n        AsyncMemoryClient._validate_api_key = _skip_validate\n        try:\n            mem0_client = AsyncMemoryClient(api_key=mem0_api_key,\n                                            host=config.host,\n                                            org_id=config.org_id,\n                                            project_id=config.project_id)\n        finally:\n            AsyncMemoryClient._validate_api_key = original_validate\n    else:\n        mem0_client = AsyncMemoryClient(api_key=mem0_api_key,\n                                        host=config.host,\n                                        org_id=config.org_id,\n                                        project_id=config.project_id)\n"""
if old not in text:
    raise SystemExit("Expected Mem0 client initialization block not found")
target.write_text(text.replace(old, new, 1))
PY

EXPOSE 8000

CMD ["sh", "-lc", "uv run --project . nat start fastapi --config_file \"${FINUS_NAT_CONFIG_FILE:-configs/router_nomemory.yml}\" --host 0.0.0.0 --port 8000"]
