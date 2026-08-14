import os
from pathlib import Path
from dotenv import load_dotenv
from mcp import StdioServerParameters

_FIN_US_ROOT = Path(__file__).resolve().parent.parent
_ROOT_ENV = _FIN_US_ROOT / ".env"

load_dotenv(_ROOT_ENV)
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
# finus_sqlite_transcript_agent thread id (HTTP header conversation-id)
NAT_CONVERSATION_ID = os.environ.get(
    "NAT_CONVERSATION_ID",
    os.environ.get("FINUS_DEFAULT_CONVERSATION_ID", "fin-us-default"),
).strip()

OLLAMA_API_KEY = os.environ.get("OLLAMA_API_KEY", "ollama")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen3.5:9b")

def _get_ollama_base_url() -> str:
    raw = os.environ.get("OLLAMA_BASE_URL", "http://host.docker.internal:11434").strip().rstrip("/")
    if not raw.endswith("/v1"):
        return f"{raw}/v1"
    return raw

OLLAMA_BASE_URL = _get_ollama_base_url()


_NEWS_MCP_DIR = Path(os.environ.get("NEWS_MCP_DIR", _FIN_US_ROOT / "mcp-news")).resolve()
_TRADING_MCP_DIR = Path(os.environ.get("TRADING_MCP_DIR", _FIN_US_ROOT / "mcp-trading")).resolve()
_DART_MCP_DIR = Path(os.environ.get("DART_MCP_DIR", _FIN_US_ROOT / "mcp-dart")).resolve()
_MCP_ENV_ALLOWED_KEYS = {
    "PATH",
    "NODE_ENV",
    "NODE_OPTIONS",
    "NODE_EXTRA_CA_CERTS",
    "SSL_CERT_FILE",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "no_proxy",
    "NAVER_CLIENT_ID",
    "NAVER_CLIENT_SECRET",
    "DART_API_KEY",
}
# mcp-trading이 읽는 KIS_* 변수를 하나씩 나열하면 새 변수가 추가될 때마다
# 화이트리스트가 누락되는 드리프트가 반복된다(#124, #127, #129, #130이 각각
# 다른 키를 놓쳤다). 개별 키 나열 대신 접두사 규칙을 써서 mcp-trading 쪽
# 소스가 유일한 진실 공급원이 되게 한다.
#
# 통과: KIS_로 시작하는 모든 변수(자식 프로세스 전용 네임스페이스)와
#       FINUS_KIS_로 시작하는 변수(TR ID 오버라이드 포함 trading 에이전트
#       설정 전반). 그 외 비밀값(DATABASE_URL 등)과 다른 FINUS_* 변수
#       (FINUS_BACKEND_URL 등 — backend/NAT 전용)는 차단된다.
#
# KIS_REAL_ORDER_ENABLED도 함께 통과한다(#129, 의도한 동작). 미설정 시
# 키 자체가 자식에 전달되지 않으므로 mcp-trading/order.js의
# validateRealOrderGuard는 fail-closed를 유지한다.
#
# finus_nat/src/nat_finus_nat/finus_api.py에 동일 목적의 화이트리스트가
# 별도 존재한다(NAT 자식 프로세스 경로) — 접두사를 바꿀 때 함께 갱신할 것.
_MCP_ENV_ALLOWED_PREFIXES = ("FIN_US_", "FINUS_KIS_", "KIS_")

# SQLite 데이터베이스 파일 경로 설정 (backend 디렉토리 내 finus.db 생성)
DATABASE_URL = os.environ.get("DATABASE_URL") or f"sqlite:///{_FIN_US_ROOT}/backend/finus.db"
DB_ECHO = os.getenv("DB_ECHO", "false").lower() == "true"

# Redis cache/lock settings for scheduler state.
REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")

# Telegram urgent alert settings.
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
VISUALIZATION_URL = os.getenv("VISUALIZATION_URL", "").strip()
KIS_ORDER_ENV = os.environ.get("KIS_ORDER_ENV", "demo").strip().lower()

def _is_truthy_flag(value: str) -> bool:
    # mcp-trading/index.js는 KIS_REAL_ORDER_ENABLED를 `=== "true"`로 비교한다 —
    # 대소문자·다른 철자(1/yes/y/TRUE 등)를 인정하지 않는다. backend가 여기서
    # 더 관대하게 받으면 backend 게이트는 통과하고 자식에서만 막히는 진단
    # 트랩이 된다(#129와 같은 증상이 철자 차이로 재발). 그래서 자식과 정확히
    # 같은 기준만 인정한다 — 이 값이 True가 되는 유일한 원본 문자열은
    # 정확히 "true"이므로, 자식에 넘길 때 다시 쓸 필요가 없다
    # (_mcp_child_env는 원본 문자열을 그대로 전달한다).
    return value.strip() == "true"


KIS_REAL_ORDER_ENABLED = _is_truthy_flag(os.environ.get("KIS_REAL_ORDER_ENABLED", ""))


def is_placeholder_secret(value: str | None) -> bool:
    if not value:
        return True
    normalized = value.strip()
    return not normalized or normalized.startswith("your_") or normalized.endswith("_here")


# CORS 설정
# 기본값은 docker-compose의 frontend(nginx)가 Unity WebGL 번들을 서빙하는 8080 오리진이다.
# #245로 nginx가 /api를 backend로 프록시하므로, 번들이 상대 경로를 쓰기 시작하면(#246,
# WebGL 재빌드 필요) 브라우저 요청은 same-origin이 되어 CORS 자체가 개입하지 않는다.
# 현재 번들은 아직 8000번을 직접 호출하므로 이 목록이 계속 필요하다.
_ALLOW_ORIGINS_RAW = os.getenv("ALLOW_ORIGINS", "http://localhost:8080,http://127.0.0.1:8080")
ALLOW_ORIGINS = [origin.strip() for origin in _ALLOW_ORIGINS_RAW.split(",") if origin.strip()]


def _mcp_child_env() -> dict[str, str]:
    return {
        key: value
        for key, value in os.environ.items()
        if key in _MCP_ENV_ALLOWED_KEYS or key.startswith(_MCP_ENV_ALLOWED_PREFIXES)
    }


def _stdio_server_params(mcp_dir: Path) -> StdioServerParameters:
    return StdioServerParameters(
        command="node",
        args=[str(mcp_dir / "index.js")],
        env=_mcp_child_env(),
        cwd=str(mcp_dir),
    )


NEWS_MCP_PARAMS = _stdio_server_params(_NEWS_MCP_DIR)
TRADING_MCP_PARAMS = _stdio_server_params(_TRADING_MCP_DIR)
DART_MCP_PARAMS = _stdio_server_params(_DART_MCP_DIR)
