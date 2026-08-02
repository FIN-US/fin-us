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
COPY scripts/finus-nat-entrypoint.sh /usr/local/bin/finus-nat-entrypoint.sh
RUN chmod +x /usr/local/bin/finus-nat-entrypoint.sh

WORKDIR /workspace/mcp-news
RUN npm ci --omit=dev

WORKDIR /workspace/mcp-trading
RUN npm ci --omit=dev

WORKDIR /workspace/mcp-dart
RUN npm ci --omit=dev

WORKDIR /workspace/finus_nat
RUN uv sync --no-dev
# 벤더(site-packages) 패치는 scripts/patch_vendor.py 단일 진입점으로 통합돼 있다.
# 로컬 실행(scripts/run.sh)과 같은 스크립트를 써서 두 경로가 갈라지지 않게 한다.
# 상류가 바뀌어 패치를 못 붙이면 스크립트가 0이 아닌 코드로 종료해 빌드가 실패한다.
# 이게 drift의 강제 지점이다. run.sh는 로컬 편의를 위해 경고만 하고 넘어간다.
#
# DL3059: uv sync와 패치를 한 RUN으로 합치면 실패 지점을 구분할 수 없어 분리한다.
# (patch_vendor.py가 위 COPY에 실려 있어 이 파일을 고치면 uv sync 레이어도 함께
#  무효화된다. 의존성 레이어 분리는 별도 과제다.)
# hadolint ignore=DL3059
RUN python scripts/patch_vendor.py --venv /workspace/finus_nat/.venv \
    --require nat/plugins/mem0ai/memory.py

EXPOSE 8000

ENTRYPOINT ["/usr/local/bin/finus-nat-entrypoint.sh"]
CMD ["sh", "-lc", "uv run --project . nat start fastapi --config_file \"${FINUS_NAT_CONFIG_FILE:-configs/router_nomemory.yml}\" --host 0.0.0.0 --port 8000"]
