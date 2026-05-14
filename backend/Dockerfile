FROM python:3.13-slim

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

WORKDIR /app

COPY backend/pyproject.toml backend/uv.lock ./backend/
RUN uv sync --project ./backend --no-dev --no-install-project --locked

COPY . .

EXPOSE 8000

CMD ["uv", "run", "--project", "./backend", "--locked", "--no-sync", "uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]
