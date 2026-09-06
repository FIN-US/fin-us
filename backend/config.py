import logging
import math
import os
from pathlib import Path
from dotenv import load_dotenv
from mcp import StdioServerParameters

logger = logging.getLogger(__name__)

_FIN_US_ROOT = Path(__file__).resolve().parent.parent
_ROOT_ENV = _FIN_US_ROOT / ".env"

load_dotenv(_ROOT_ENV)
load_dotenv()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
OPENAI_CHAT_MODEL = os.getenv("OPENAI_CHAT_MODEL", "gpt-5.4-mini")
ANTHROPIC_CHAT_MODEL = os.getenv("ANTHROPIC_CHAT_MODEL", "claude-sonnet-4-20250514")

# 호스트에서 백엔드만 띄울 때 쓰이는 기본값이다(#305). finus-nat은 컴포즈가
# 127.0.0.1:8001에 게시하므로(#285) 8001을 가리킨다 — 8000은 백엔드 자신이 듣는
# 포트라 기본값이 자기 자신에게 /v1/chat/completions를 부르는 꼴이 된다.
# 컨테이너 안에서는 docker-compose.yml의 environment가 finus-nat:8000으로 덮는다.
NAT_BASE_URL = os.environ.get("NAT_BASE_URL", "http://127.0.0.1:8001").rstrip("/")
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

# 신호 유의성 점수화 (#298).
# 2차 필터(services.score_signal)가 YES/NO 대신 -3~+3 정수를 받는다.
# 레인지를 좁게 잡은 것은 의도다 — 경량 모델은 0~100 같은 넓은 축에서 재현성이 없다.
SIGNAL_SCORE_MIN = -3
SIGNAL_SCORE_MAX = 3


def _warn_bad_env(name: str, raw: str, default: object) -> None:
    """설정 env를 해석하지 못해 기본값으로 되돌아간 사실을 경고로 남긴다.

    '기본값 복귀'는 앱이 죽지 않게 하지만, 되돌아갔다는 사실 자체가 조용하면 안 된다.
    한도(#299)에서는 **기본값이 곧 가장 넓은 값**이라 특히 그렇다 —
    ``ORDER_MAX_ORDER_AMOUNT=500,000``처럼 쉼표를 넣으면 ``int()``가 실패해 기본값
    100만원, 즉 의도한 한도의 두 배가 걸린다. 임계값(#298)에서는 반대로 감시가
    조용히 침묵하는 쪽으로 샌다. 같은 레포의 다른 fail-open 지점(stock_code의 마스터
    로드, scheduler의 잔고 동기화)이 모두 경고를 남기는 것과 같은 이유다.
    """
    logger.warning(
        "%s 값을 해석하지 못해 기본값 %s를 사용합니다 (입력=%r). 의도한 설정이 "
        "적용되지 않으니 값을 확인하세요.",
        name,
        default,
        raw,
    )


def _int_env_in_range(name: str, default: int, low: int, high: int) -> int:
    """정수 env를 [low, high]로 강제한다. 값이 없거나 이상하면 default로 되돌린다.

    범위를 벗어난 임계값은 조용히 파이프라인을 망가뜨린다 — 0이면 모든 signal이
    유의미해져 필터가 사라지고, 4 이상이면 어떤 점수도 통과하지 못해 감시가
    영구히 침묵한다. 둘 다 "놓침 방지"(REQ-04) 관점에서 사고이므로 받아들이지 않는다.
    """
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        _warn_bad_env(name, raw, default)
        return default
    if not low <= value <= high:
        _warn_bad_env(name, raw, default)
        return default
    return value


# |score| >= 이 값이면 유의미로 판정한다. 1이면 약한 호재/악재까지 상세 분석을 태우고,
# 3이면 대형 이벤트만 통과한다. 기본 2 = "방향이 분명한" 신호부터.
SIGNAL_SCORE_THRESHOLD = _int_env_in_range("SIGNAL_SCORE_THRESHOLD", 2, 1, SIGNAL_SCORE_MAX)


def _int_env(name: str, default: int) -> int:
    """정수 env를 읽되, 값이 비었거나 정수가 아니면 기본값으로 되돌린다.

    오타 하나로 한도가 0이 되거나(모든 주문 거부) 예외로 앱이 죽는 대신
    '기본값 복귀'로 고정하되, 되돌아간 사실은 경고로 남긴다.
    """
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        _warn_bad_env(name, raw, default)
        return default


def _float_env(name: str, default: float) -> float:
    """실수 env를 읽되, 해석 불가·비유한·음수면 기본값으로 되돌린다.

    #298(임계값)과 #299(한도)가 **같은 함수 하나**를 쓴다. 병합 과정에서 두 벌이
    생겼다가 뒤엣것이 앞엣것을 가리는 상태가 됐는데, 그때 ORDER_* 실수 설정들만
    isfinite·음수 가드를 잃었다. 비중·비율 한도에 ``inf``나 음수가 들어가면 그 한도가
    사실상 사라지므로, 두 쓰임 모두에 같은 가드가 필요하다.
    """
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        _warn_bad_env(name, raw, default)
        return default
    # isfinite로 inf/nan을 함께 막는다. inf를 통과시키면 "기사 간 평가 엇갈림"이
    # 영구히 침묵하고(#298), 비율 한도에서는 그 한도가 없는 것과 같아진다(#299).
    # 설정이 조용히 기능 하나를 끄는 것 — _int_env_in_range가 배격한 것과 같은 유형이다.
    if not math.isfinite(value) or value < 0:
        _warn_bad_env(name, raw, default)
        return default
    return value


# 기사별 점수의 표준편차가 이 값 이상이면 "기사 간 평가 엇갈림"을 알림에 표시한다.
# 기본 1.0 = 7단계 축에서 기사들이 한 칸 넘게 흩어졌다는 뜻.
SIGNAL_UNCERTAINTY_ALERT_THRESHOLD = _float_env("SIGNAL_UNCERTAINTY_ALERT_THRESHOLD", 1.0)


# 임계값 미만으로 걸러진 신호의 채점 기록(models.FilteredSignal, #304)을 며칠 보관할지.
# 감시 루프가 종목·소스마다 10분 주기로 돌아 행이 빠르게 쌓이므로 무기한 보관하지
# 않는다. 기본 30일은 임계값을 한 번 조정하고 그 효과를 관찰하는 주기를 덮으면서도
# SQLite 한 파일이 감당할 수 있는 크기다.
# 상한 365일: 그보다 오래 보관해야 할 이유가 생겼다면 SQLite가 아니라 별도 저장소를
# 검토할 시점이라는 뜻이다. 하한 1일: 0을 허용하면 기록하자마자 지워져 기능이 조용히
# 꺼진다 — _int_env_in_range가 임계값에서 배격한 것과 같은 실패 유형이다.
FILTERED_SIGNAL_RETENTION_DAYS = _int_env_in_range("FILTERED_SIGNAL_RETENTION_DAYS", 30, 1, 365)


# Redis cache/lock settings for scheduler state.
# 기본값이 `localhost`가 아닌 것은 compose의 redis 게시가 루프백 IPv4 전용이기
# 때문이다(#285). `localhost`가 `::1`로 먼저 풀리는 호스트에서 주소 폴백에 기대지
# 않는다. `.env.example`과 어긋나면 test_compose_ports.py가 잡는다.
REDIS_URL = os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0")

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


# ──────────────────────────────────────────────────────────────────────────
# 주문 보조(#299) 하드 한도 — 코드가 판정하는 값들
# ──────────────────────────────────────────────────────────────────────────
# 이 상수들은 LLM이 볼 수는 있어도 바꿀 수는 없다. 검증 에이전트에는 참고용으로
# 함께 전달되지만, 위반 여부를 판정하는 것은 backend/order_assist.py의 순수 함수다.
# 전부 env로 덮어쓸 수 있게 두되 기본값은 보수적으로 잡는다 — 미설정 환경에서
# 한도가 사라지는 것이 아니라 좁은 한도가 걸리는 쪽이 안전하다.
#
# 읽는 함수(_int_env / _float_env / _warn_bad_env)는 이 파일 위쪽, #298의
# 임계값 설정과 같은 자리에 있다. 같은 이름의 함수를 여기 한 벌 더 두면 뒤엣것이
# 앞엣것을 가려 어느 쪽 가드가 걸리는지가 정의 순서에 좌우된다.


# 1회 주문 금액 상한(원).
ORDER_MAX_ORDER_AMOUNT = _int_env("ORDER_MAX_ORDER_AMOUNT", 1_000_000)
# 한 종목이 총 평가금액에서 차지할 수 있는 비중 상한(0~1).
ORDER_MAX_POSITION_RATIO = _float_env("ORDER_MAX_POSITION_RATIO", 0.20)
# 하루 주문 횟수 상한(TradeHistory 집계).
ORDER_MAX_DAILY_COUNT = _int_env("ORDER_MAX_DAILY_COUNT", 10)
# 하루 거래대금 상한(원, TradeHistory 집계).
ORDER_MAX_DAILY_AMOUNT = _int_env("ORDER_MAX_DAILY_AMOUNT", 3_000_000)
# 주문 후 남아야 하는 현금 비중 하한(총 평가금액 대비, 0~1).
ORDER_MIN_CASH_RATIO = _float_env("ORDER_MIN_CASH_RATIO", 0.10)
# 지정가가 현재가에서 벗어날 수 있는 최대 비율(0~1).
ORDER_MAX_PRICE_GAP_RATIO = _float_env("ORDER_MAX_PRICE_GAP_RATIO", 0.03)
# 제안 확신도 하한. soft — 위반해도 코드가 거부하지 않고 검증자 참고용으로만 전달한다.
ORDER_MIN_CONFIDENCE = _float_env("ORDER_MIN_CONFIDENCE", 0.6)
# 같은 종목+룰 조합의 재제안 냉각 시간(분). hard — redis TTL로 강제한다.
ORDER_REPROPOSAL_COOLDOWN_MINUTES = _int_env("ORDER_REPROPOSAL_COOLDOWN_MINUTES", 60)

# 주문 금지 종목코드 목록(쉼표 구분). 기본은 빈 목록이다.
# 판정은 order_assist가 is_orderable_stock_code_strict() 검사 바로 옆에서 수행한다 —
# 둘 다 "이 코드로 주문을 낼 수 있는가"라는 같은 질문의 두 갈래라 떨어뜨려 두면
# 한쪽만 통과하는 경로가 생긴다.
ORDER_BLACKLIST = frozenset(
    code.strip().upper()
    for code in os.environ.get("ORDER_BLACKLIST", "").split(",")
    if code.strip()
)

# NAT 주문 보조 엔드포인트 호출 상한(초). 제안은 ReAct 루프라 길고, 검증은 단발 호출이라 짧다.
# 검증 타임아웃은 fail-closed로 이어진다 — 초과 시 REJECT다.
ORDER_PROPOSE_TIMEOUT_SECONDS = _float_env("ORDER_PROPOSE_TIMEOUT_SECONDS", 120.0)
ORDER_VERIFY_TIMEOUT_SECONDS = _float_env("ORDER_VERIFY_TIMEOUT_SECONDS", 40.0)


# ──────────────────────────────────────────────────────────────────────────
# 스케줄러 룰 트리거(#314) — 감시 신호가 자동 제안을 부르는 조건
# ──────────────────────────────────────────────────────────────────────────
# 위의 하드 한도가 "제안을 어디까지 받아들일 것인가"라면, 이쪽은 "제안을 언제 만들 것인가"다.
# 판정은 여전히 order_assist가 하고, 여기 값은 그 함수를 부를지 말지만 정한다.
#
# 기본값이 "꺼짐"인 것은 위 한도들의 "보수적 기본값"과 같은 원칙의 다른 적용이다. 한도는
# 미설정 시 좁게 걸리는 쪽이 안전하고, 자동 제안은 미설정 시 아예 돌지 않는 쪽이 안전하다 —
# 사용자가 켠 적 없는데 확정 버튼이 뜨는 것은 어떤 한도로도 되돌릴 수 없다.

# 자동 제안 켜기. _is_truthy_flag는 정확히 "true"만 인정한다 — 오타·다른 철자(1/yes/TRUE)가
# 자동 주문 제안을 켜는 방향으로 해석되지 않게 하려는 것이고, 여기서는 그 엄격함이 그대로
# 안전한 방향이다.
ORDER_RULE_TRIGGER_ENABLED = _is_truthy_flag(
    os.environ.get("ORDER_RULE_TRIGGER_ENABLED", "")
)

# 자동 제안을 부르는 신호 소스(쉼표 구분). scheduler.SIGNAL_SOURCES의 name과 같은 값이다.
# 알 수 없는 이름을 적으면 그 이름은 어떤 신호와도 매칭되지 않아 조용히 무시된다 —
# 매칭 실패는 "제안하지 않음"이므로 fail-closed 방향이다.
ORDER_RULE_SOURCES = frozenset(
    name.strip().lower()
    for name in os.environ.get("ORDER_RULE_SOURCES", "news,disclosure").split(",")
    if name.strip()
)

# 자동 제안을 부르는 긴급도(쉼표 구분). schemas.UrgencyLevel의 값이다.
# 기본은 critical 하나뿐이다. 텔레그램 긴급 알림 기준(high 이상)보다 좁게 잡는 이유는
# 알림과 주문 제안의 대가가 다르기 때문이다 — 알림은 읽고 넘기면 되지만 제안은 확정 버튼과
# 대기 주문 슬롯을 차지하고, 그 슬롯은 사용자가 직접 치는 /buy와 공유된다.
ORDER_RULE_URGENCY_LEVELS = frozenset(
    level.strip().lower()
    for level in os.environ.get("ORDER_RULE_URGENCY_LEVELS", "critical").split(",")
    if level.strip()
)


def is_placeholder_secret(value: str | None) -> bool:
    if not value:
        return True
    normalized = value.strip()
    return not normalized or normalized.startswith("your_") or normalized.endswith("_here")


# API 정적 키(#266 2단계). 값이 있으면 `/api/` 아래 모든 HTTP 요청과 `/api/v1/ws`
# 핸드셰이크가 이 키를 요구한다(backend/main.py). 방식은 #266의 방향 결정 코멘트를
# 그대로 따른다 — REST는 헤더, WS는 쿼리 파라미터(브라우저 WS API가 커스텀 헤더를 못
# 붙인다), 키는 .env로 관리.
#
# **기본값이 빈 문자열 = 인증 꺼짐이다.** 이 저장소가 보안 설정에서 보통 고르는
# fail-closed와 반대 방향이라 이유를 남긴다. 지금 추적 중인 Unity WebGL 번들
# (frontend/Build)은 키를 실어 보내지 못한다 — ApiClient가 헤더를 붙이지 않고, 붙이게
# 하려면 WebGL 재빌드와 Build/ 커밋이 따라온다(frontend/README.md). 기본값을 "켜짐"으로
# 두면 `docker compose up`이 그대로 401 화면이 되고, 되돌리는 스위치가 코드가 아니라
# .env에만 있어 원인을 찾기 어렵다. 그래서 켜는 것을 운영자의 명시적 행위로 둔다.
#
# 꺼져 있다는 사실 자체는 조용하지 않다 — main.py의 lifespan이 기동 로그에 경고를
# 남긴다. "설정이 조용히 기능 하나를 끄는" 상태를 배격하는 것은 위 _int_env_in_range와
# 같은 원칙이다.
#
# 자리표시자(`your_..._here`)는 미설정으로 본다. 그대로 유효한 키로 인정하면 .env.example에
# 적힌 값으로 열리는, 켜져 있는데 아무나 아는 키인 상태가 된다 — 안 켜진 것보다 나쁘다.
_API_KEY_RAW = os.getenv("FINUS_API_KEY", "")
FINUS_API_KEY = "" if is_placeholder_secret(_API_KEY_RAW) else _API_KEY_RAW.strip()


# WebSocket(/api/v1/ws) 핸드셰이크의 Origin 허용목록.
# 기본값은 docker-compose의 frontend(nginx)가 Unity WebGL 번들을 서빙하는 8080 오리진이다.
# 원래는 CORS 설정을 겸했지만, #245로 nginx가 /api를 backend로 프록시하고 #246·#262로
# 번들이 상대 경로를 쓰도록 재빌드되면서 브라우저 요청이 same-origin이 됐고, 쓰이지 않게
# 된 CORSMiddleware는 #246에서 제거했다.
# 목록 자체는 남는다 — CORSMiddleware는 WebSocket 핸드셰이크에 적용되지 않아, /api/v1/ws가
# 이 목록을 직접 읽어 Origin을 대조하기 때문이다(#256, main.py is_allowed_ws_origin).
# 지금은 그 검사가 유일한 소비자이므로, 지우면 화면은 뜨는데 실시간 알림만 403으로 끊긴다.
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
