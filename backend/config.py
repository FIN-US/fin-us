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
# mcp-trading이 읽는 KIS_* 변수 집합(index.js, order-dedup.js)을 하나씩 나열하면
# 새 변수가 추가될 때마다 화이트리스트가 누락되는 드리프트가 반복된다
# (#124 → #127에서 KIS_ORDER_DEDUP_*를 놓쳤고, #130에서 KIS_TOKEN_CACHE_PATH·
# KIS_TR_ID_*·FINUS_KIS_TR_ID_*를, #129에서 KIS_REAL_ORDER_ENABLED를 놓쳤다).
# 그래서 개별 KIS_* 키 나열을 접두사 규칙으로 대체해 mcp-trading 쪽 소스가
# 유일한 진실 공급원이 되게 한다 — 새 KIS_* 변수를 mcp-trading이 추가로 읽게 되면
# 이 파일을 고치지 않아도 자동으로 전달된다.
#
# 통과: KIS_로 시작하는 모든 변수(KIS_API_KEY, KIS_API_SECRET, KIS_ACCOUNT_NO,
#       KIS_URL, KIS_ORDER_ENV, KIS_REAL_ORDER_ENABLED, KIS_ORDER_DEDUP_PATH,
#       KIS_ORDER_DEDUP_TTL_MS, KIS_TOKEN_CACHE_PATH, KIS_TR_ID_* 등)과
#       FINUS_KIS_로 시작하는 변수(FINUS_KIS_TR_ID_DAILY_CCLD,
#       FINUS_KIS_TR_ID_BALANCE_RLZ_PL 등 — mcp-trading/index.js:46,54의
#       TR ID 오버라이드)만 자식 프로세스로 전달한다.
# 차단: KIS_로 시작하지 않는 나머지 모든 비밀값(DATABASE_URL, OPENAI_API_KEY,
#       ANTHROPIC_API_KEY, TELEGRAM_BOT_TOKEN 등)과, KIS_ 접두사가 아닌 다른
#       FINUS_* 변수(FINUS_BACKEND_URL, FINUS_MEM0_* 등 — backend/NAT 전용,
#       mcp-trading이 읽지 않음)는 접두사 매칭 대상이 아니므로 여전히 차단된다.
#
# KIS_REAL_ORDER_ENABLED는 이 접두사 규칙으로 함께 통과한다(#129). 주문 멱등
# 원장 경로 전달(#127, 위 KIS_ORDER_DEDUP_* 통과 확인)이 선행 조건이었고 이미
# 충족되어 있다. 미설정 시에는 os.environ에 키 자체가 없으므로 자식에도 전달되지
# 않고, mcp-trading/index.js:344·order.js:56-61 가드는 `undefined === "true"`가
# false이므로 fail-closed(실계좌 주문 차단)를 유지한다.
#
# finus_nat/src/nat_finus_nat/finus_api.py:28-49 에 같은 목적의 화이트리스트가
# 별도로 존재한다(자식 프로세스로 mcp-trading 등을 직접 실행하는 경로). 이
# 접두사 목록을 바꿀 때는 그쪽도 함께 갱신할 것.
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

_TRUTHY_FLAG_VALUES = {"1", "true", "yes", "y"}


def _is_truthy_flag(value: str) -> bool:
    return value.strip().lower() in _TRUTHY_FLAG_VALUES


KIS_REAL_ORDER_ENABLED = _is_truthy_flag(os.environ.get("KIS_REAL_ORDER_ENABLED", ""))


def is_placeholder_secret(value: str | None) -> bool:
    if not value:
        return True
    normalized = value.strip()
    return not normalized or normalized.startswith("your_") or normalized.endswith("_here")


# CORS 설정
_ALLOW_ORIGINS_RAW = os.getenv("ALLOW_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173")
ALLOW_ORIGINS = [origin.strip() for origin in _ALLOW_ORIGINS_RAW.split(",") if origin.strip()]


def _mcp_child_env() -> dict[str, str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if key in _MCP_ENV_ALLOWED_KEYS or key.startswith(_MCP_ENV_ALLOWED_PREFIXES)
    }
    # backend는 KIS_REAL_ORDER_ENABLED를 1/true/yes/y(대소문자 무관)로 넓게
    # 받지만(_is_truthy_flag), mcp-trading/index.js는 `=== "true"` 엄격 비교라
    # 정규화하지 않으면 backend 게이트는 통과하고 자식에서만 막히는 진단 트랩이
    # 된다(#129와 같은 증상). 자식에는 항상 정확히 "true"/"false"만 넘긴다.
    if "KIS_REAL_ORDER_ENABLED" in env:
        env["KIS_REAL_ORDER_ENABLED"] = "true" if _is_truthy_flag(env["KIS_REAL_ORDER_ENABLED"]) else "false"
    return env


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
