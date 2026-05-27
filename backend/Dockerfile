FROM python:3.13-slim

# 파이프 도중 실패(curl 등)를 다음 명령으로 무시하지 않도록 bash pipefail 사용
SHELL ["/bin/bash", "-o", "pipefail", "-c"]

# DL3008: python:3.13-slim의 Debian 저장소는 패키지 구버전을 주기적으로 제거해 버전 핀은 오히려 재현성을 해친다.
# hadolint ignore=DL3008
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates gnupg && \
    curl -fsSL https://deb.nodesource.com/setup_24.x | bash - && \
    apt-get install -y --no-install-recommends nodejs && \
    rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.9.28 /uv /uvx /bin/

WORKDIR /opt/mcp-news
COPY mcp-news/package*.json ./
RUN npm ci --omit=dev
COPY mcp-news ./

WORKDIR /opt/mcp-trading
COPY mcp-trading/package*.json ./
RUN npm ci --omit=dev
COPY mcp-trading ./

WORKDIR /opt/mcp-dart
COPY mcp-dart/package*.json ./
RUN npm ci --omit=dev
COPY mcp-dart ./

ENV NEWS_MCP_DIR=/opt/mcp-news \
    TRADING_MCP_DIR=/opt/mcp-trading \
    DART_MCP_DIR=/opt/mcp-dart

WORKDIR /app

COPY backend/pyproject.toml backend/uv.lock ./backend/
RUN uv sync --project ./backend --no-dev --no-install-project --locked

COPY . .

EXPOSE 8000

CMD ["uv", "run", "--project", "./backend", "--locked", "--no-sync", "uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8000"]
