FROM python:3.11-slim

RUN apt-get update && apt-get install -y curl gnupg && \
    curl -fsSL https://deb.nodesource.com/setup_20.x | bash - && \
    apt-get install -y nodejs && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY mcp-news/package*.json ./mcp-news/
RUN cd ./mcp-news && npm install

COPY mcp-trading/package*.json ./mcp-trading/
RUN cd ./mcp-trading && npm install

RUN cd ./mcp-news && npx playwright install --with-deps chromium

COPY backend/requirements.txt ./backend/
RUN pip install --no-cache-dir -r ./backend/requirements.txt

COPY . .

WORKDIR /app
EXPOSE 8000

CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]
