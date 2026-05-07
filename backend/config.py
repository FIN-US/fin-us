import os
from pathlib import Path
from dotenv import load_dotenv
from mcp import StdioServerParameters

_FIN_US_ROOT = Path(__file__).resolve().parent.parent
_BACKEND_ENV = _FIN_US_ROOT / "backend" / ".env"

load_dotenv(_BACKEND_ENV)
load_dotenv()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
OPENAI_CHAT_MODEL = os.getenv("OPENAI_CHAT_MODEL", "gpt-5.4-mini")
ANTHROPIC_CHAT_MODEL = os.getenv("ANTHROPIC_CHAT_MODEL", "claude-sonnet-4-20250514")

NAT_BASE_URL = os.environ.get("NAT_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
NAT_CHAT_MODEL = os.environ.get(
    "NAT_CHAT_MODEL",
    os.environ.get("OPENAI_CHAT_MODEL", "gpt-5.4-mini"),
)

OLLAMA_API_KEY = os.environ.get("OLLAMA_API_KEY", "ollama")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen3.5:9b")
OLLAMA_BASE_URL = os.environ.get("OLLAMA_BASE_URL", "http://host.docker.internal:11434")


_NEWS_MCP_DIR = (_FIN_US_ROOT / "mcp-news").resolve()
_TRADING_MCP_DIR = (_FIN_US_ROOT / "mcp-trading").resolve()

# SQLite 데이터베이스 파일 경로 설정 (backend 디렉토리 내 finus.db 생성)
DATABASE_URL = os.environ.get("DATABASE_URL") or f"sqlite:///{_FIN_US_ROOT}/backend/finus.db"
DB_ECHO = os.getenv("DB_ECHO", "false").lower() == "true"

# CORS 설정
_ALLOW_ORIGINS_RAW = os.getenv("ALLOW_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173")
ALLOW_ORIGINS = [origin.strip() for origin in _ALLOW_ORIGINS_RAW.split(",") if origin.strip()]


def _stdio_server_params(mcp_dir: Path) -> StdioServerParameters:
    return StdioServerParameters(
        command="node",
        args=[str(mcp_dir / "index.js")],
        cwd=str(mcp_dir),
    )


NEWS_MCP_PARAMS = _stdio_server_params(_NEWS_MCP_DIR)
TRADING_MCP_PARAMS = _stdio_server_params(_TRADING_MCP_DIR)
