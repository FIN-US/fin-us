FROM python:3.13-slim

RUN apt-get update && apt-get install -y curl gnupg && \
    curl -fsSL https://deb.nodesource.com/setup_24.x | bash - && \
    apt-get install -y nodejs && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /opt/mcp-news
COPY mcp-news/package*.json ./
RUN npm ci --omit=dev
COPY mcp-news ./

WORKDIR /opt/mcp-trading
COPY mcp-trading/package*.json ./
RUN npm ci --omit=dev
COPY mcp-trading ./

WORKDIR /app

COPY backend/requirements.lock ./backend/
RUN pip install --no-cache-dir -r ./backend/requirements.lock

COPY . .

WORKDIR /app
EXPOSE 8000

CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]
