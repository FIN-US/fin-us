FROM python:3.13-slim

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

EXPOSE 8000

CMD ["uv", "run", "--project", ".", "nat", "start", "fastapi", \
     "--config_file", "configs/router.yml", \
     "--host", "0.0.0.0", \
     "--port", "8000"]
