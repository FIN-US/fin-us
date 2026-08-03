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
    #
    # 아래 시점의 env[...]를 다시 파싱하는 대신 위쪽 모듈 스코프의
    # KIS_REAL_ORDER_ENABLED(bool) 상수를 직접 쓰고 싶어질 수 있는데, 그러면
    # 안 된다: 그 상수는 import 시점 os.environ 스냅샷으로 한 번만 계산되고,
    # env 딕셔너리는 매 호출 os.environ.items()를 다시 읽는다. 테스트가
    # monkeypatch.setenv로 import 이후 값을 바꾸므로(reload 없이), 모듈 상수를
    # 쓰면 그 시점에는 여전히 옛 값이라 여기서만 값이 갈린다. 운영에서는
    # os.environ이 프로세스 수명 동안 바뀌지 않아 둘이 항상 같지만, 이 재파싱
    # 방식이 테스트가 이미 전제하는 "매 호출 os.environ을 다시 읽는다"는
    # 동작과 일치해 더 안전하다.
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
