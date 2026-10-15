"""텔레그램 명령의 사용자 문구와 콜백 데이터 접두사."""


from backend.trading.orders import PENDING_ORDER_NEXT_STEP_TEXT


ALERT_COMMAND_HELP = "사용법: /alerts urgent | all | off | status"
LEVEL_COMMAND_HELP = "사용법: /level 초보 | 중급"
LEVEL_ONBOARDING_QUESTION = "주식 투자 얼마나 익숙하세요?"
# 온보딩은 자기 신고 문항 두 개(수준·투자 성향)가 전부다. 지식 퀴즈로 수준을 판정하지 않는다 —
# 봇이 사용자를 시험하는 관계가 되는 순간 설명을 켜 두는 것이 창피한 일이 되고, 그러면 초보 모드가
# 쓰이지 않는다. 두 문항은 메시지를 나눈다: 버튼 묶음이 메시지마다 하나라, 한 메시지에 합치면
# 어느 줄이 어느 질문의 답인지 흐려진다.
START_MESSAGE = "\n".join(
    [
        "안녕하세요. 이 봇은 시세·공시·매매를 텔레그램에서 다룹니다.",
        "",
        LEVEL_ONBOARDING_QUESTION,
        "초보를 고르면 낯선 용어에 한 줄 설명이 붙습니다. 나중에 /level 로 바꿀 수 있어요.",
    ]
)
RISK_COMMAND_HELP = "사용법: /risk 안정형 | 공격형 | 해제"
RISK_ONBOARDING_QUESTION = "투자할 때 어느 쪽에 더 가까우세요?"
# 성향은 종목 추천의 논조만 바꾼다(#397). 고르지 않아도 되는 문항이라는 점을 함께 적는다 —
# 미설정이 기존 추천과 같은 동작이다.
START_RISK_MESSAGE = "\n".join(
    [
        RISK_ONBOARDING_QUESTION,
        "안정형은 종목 추천에서 변동성 위험을 먼저, 공격형은 기회 요인을 먼저 짚어 드립니다.",
        "고르지 않으면 지금처럼 성향 구분 없이 추천합니다. 나중에 /risk 로 바꾸거나 해제할 수 있어요.",
    ]
)
RISK_MEMORY_DISABLED_TEXT = (
    "사용자 메모리가 꺼져 있어 투자 성향을 저장하거나 불러올 수 없습니다. "
    "종목 추천은 성향 구분 없이 동작합니다."
)
BUY_COMMAND_HELP = "사용법: /buy <종목명> <수량> [지정가]"
SELL_COMMAND_HELP = "사용법: /sell <종목명> <수량> [지정가]"
WATCH_COMMAND_HELP = "사용법: /watch add <종목명> | remove <종목명> | list"
CATALYST_COMMAND_HELP = "사용법: /catalysts <종목명>"
ADVISE_COMMAND_HELP = "사용법: /advise <종목명>"
EARNINGS_COMMAND_HELP = "사용법: /earnings <종목명> [기간]  예: /earnings 삼성전자 2025Q1"
NATURAL_ORDER_HELP = "자연어 주문을 해석할 수 없습니다. /buy 또는 /sell 형식으로 입력하세요."
# 미등록 코드는 mcp/trading/stock-master.js가 {code: X, name: X, market: "UNKNOWN"}을
# echo하므로 종목명 == 종목코드가 곧 미해석 신호다.
UNRESOLVED_STOCK_WARNING = (
    "⚠️ 종목명을 확인하지 못했습니다. 입력한 코드가 맞는지 다시 확인하세요."
)
# _STOCK_CODE_EXTRACT_RE, _ORDERABLE_STOCK_CODE_RE → backend/trading/stock_code.py (#140)
# ORDER_CONFIRM_CALLBACK, ORDER_CANCEL_CALLBACK, order_reply_markup → trading/orders.py (#314).
# 스케줄러의 자동 제안도 같은 버튼을 써야 해서 옮겼다. 이름은 여기서 계속 읽을 수 있다.
ORDER_STALE_CALLBACK_TEXT = "이전 주문 버튼입니다. 최신 주문 메시지에서 다시 선택하세요."
# 재시작 전에 처리를 시작한 /confirm이 재배달됐을 때의 안내 (#383). 원래 실행의 결과 메시지가
# 사라졌을 수 있으므로 직전 주문 확인을 함께 권한다.
CONFIRM_REPLAY_REFUSED_TEXT = (
    "이 /confirm은 재시작 전에 이미 처리를 시작한 요청이라 다시 실행하지 않았습니다.\n"
    "직전 주문 결과는 증권사 앱에서 확인하고, 확정할 대기 주문이 남아 있으면 "
    # 이 안내는 대기 주문을 읽기 전에 나간다. 자동 제안일 수도 있으므로 버튼을 먼저 권한다(PR #391 리뷰).
    "주문 메시지의 확정 버튼을 누르세요. 직접 낸 주문은 /confirm을 새로 보내도 됩니다."
)
# 새 주문이 기존 대기 주문에 막혔을 때의 안내 (PR #391 리뷰). 기존 주문의 출처를 모르므로 두 출처를
# 다 덮는 문장을 쓴다(PENDING_ORDER_NEXT_STEP_TEXT).
PENDING_ORDER_CONFLICT_TEXT = f"이미 대기 중인 주문이 있습니다. {PENDING_ORDER_NEXT_STEP_TEXT}"
# 텍스트 /confirm이 지금 대기 주문의 확정 프롬프트보다 먼저 보낸 것일 때의 안내 (#386). 폴러
# 적체·다운타임 뒤 늦게 처리된 /confirm이나 /buy 처리 중에 미리 보낸 /confirm이 여기 온다.
# 대기 주문은 그대로 남으므로 내용을 보고 다시 확정하면 된다.
CONFIRM_BEFORE_PROMPT_TEXT = (
    "이 /confirm은 현재 대기 주문이 표시되기 전에 보낸 것이라 실행하지 않았습니다.\n"
    "주문 내용을 확인하고 /confirm을 다시 보내거나 주문 메시지의 확정 버튼을 누르세요."
)
# 대기 주문의 확정 프롬프트 id를 모를 때의 안내 (#386). 프롬프트가 아직 전송 중이거나, id를
# 읽거나 남기지 못한 경우다. 후자는 다시 보낸 /confirm도 같은 이유로 막히므로 버튼을 권한다.
CONFIRM_PROMPT_UNKNOWN_TEXT = (
    "이 대기 주문은 안내 메시지가 언제 표시됐는지 확인할 수 없어 /confirm으로 실행하지 "
    "않았습니다.\n주문 메시지를 확인하고 확정 버튼을 누르세요."
)
# 자동 제안 주문에 텍스트 /confirm이 닿았을 때의 안내 (#390). 사용자 명령 없이 생긴 주문은
# 확정 버튼으로만 확정한다. 대기 주문은 그대로 남으므로 그 메시지의 버튼을 누르면 된다.
# 출처를 모르는 저장값도 자동 제안으로 간주해 여기로 온다(fail-closed).
CONFIRM_AUTO_PROPOSAL_BUTTON_ONLY_TEXT = (
    "이 대기 주문은 자동 제안이라 /confirm으로 실행하지 않았습니다.\n"
    "주문 내용을 확인하고 그 메시지의 확정 버튼을 누르세요. 취소하려면 /cancel을 보내세요."
)
ALERT_CALLBACK_PREFIX = "alerts:"
LEVEL_CALLBACK_PREFIX = "level:"
RISK_CALLBACK_PREFIX = "risk:"
BALANCE_REFRESH_CALLBACK = "balance:refresh"
TRADE_CALLBACK_PREFIX = "trade:"
LOOKUP_CALLBACK_PREFIX = "lookup:"
WATCH_LIST_CALLBACK = "watch:list"
WATCH_LIST_QUOTE_DELAY_SECONDS = 1.1
MARKET_QUOTE_CALLBACK = "market:quote"
MARKET_TREND_CALLBACK = "market:trend"
MARKET_TREND_DETAIL_CALLBACK = "market:trend_detail"
MARKET_STALE_CALLBACK_TEXT = "이전 조회 버튼입니다. 최신 조회 메시지에서 다시 선택하세요."
MARKET_CALLBACK_LIMIT = 100


ALERT_MODE_EMOJIS = {
    "urgent": "🚨",
    "all": "📣",
    "off": "🔕",
}
TELEGRAM_INTERACTIVE_HELP = "\n".join(
    [
        "사용 가능한 명령:",
        "/alerts urgent|all|off|status - Telegram 알림 모드 변경",
        "/level 초보|중급 - 용어 설명 표시 수준 변경",
        "/risk 안정형|공격형|해제 - 종목 추천 투자 성향 설정",
        "/balance - 예수금·총자산·보유 종목 조회",
        "/watch add <종목명>|remove <종목명>|list - 관심 종목 관리",
        "/catalysts <종목명> - 예정 촉매 이벤트 조회",
        "/trade - 매수·매도 주문 입력 안내",
        "/lookup - 현재가·수급 조회 입력 안내",
        "/visualize - Unity 포트폴리오 시각화 링크",
        "/quote <종목명> - 현재가 조회",
        "/trend <종목명> - 외국인·기관·개인 수급 조회",
        "/earnings <종목명> [기간] - DART 실적·뉴스 분석",
        "/advise <종목명> - 주문 보조: 제안 → 한도 검사 → 검증 → 확정 버튼",
        "/buy <종목명> <수량> [지정가] - 매수 주문 준비",
        "/sell <종목명> <수량> [지정가] - 매도 주문 준비",
        "/confirm - 대기 주문 확정",
        "/cancel - 대기 주문 취소",
        "일반 문장은 NAT에게 바로 질문합니다.",
    ]
)
TELEGRAM_BOT_COMMANDS = [
    {"command": "help", "description": "사용 가능한 명령 확인"},
    {"command": "balance", "description": "예수금·총자산·보유 종목 조회"},
    {"command": "watch", "description": "관심 종목 추가·삭제·조회 (add/remove/list)"},
    {"command": "catalysts", "description": "예정 촉매 이벤트 조회"},
    {"command": "quote", "description": "종목 현재가 조회"},
    {"command": "trend", "description": "종목 외국인·기관·개인 수급 조회"},
    {"command": "earnings", "description": "DART 실적·뉴스 분석"},
    {"command": "alerts", "description": "Telegram 알림 모드 변경"},
    {"command": "level", "description": "용어 설명 표시 수준 변경 (초보/중급)"},
    {"command": "risk", "description": "종목 추천 투자 성향 설정 (안정형/공격형)"},
    {"command": "start", "description": "봇 소개와 수준·투자 성향 설정"},
    {"command": "visualize", "description": "Unity 포트폴리오 시각화 링크"},
    {"command": "trade", "description": "매수·매도 주문 입력 안내"},
    {"command": "lookup", "description": "현재가·수급 조회 입력 안내"},
    {"command": "advise", "description": "주문 보조 (예: /advise 삼성전자)"},
    {"command": "buy", "description": "매수 주문 준비 (예: /buy 삼성전자 1)"},
    {"command": "sell", "description": "매도 주문 준비 (예: /sell 삼성전자 1)"},
    {"command": "confirm", "description": "대기 주문 확정"},
    {"command": "cancel", "description": "대기 주문 취소"},
]
QUOTE_COMMAND_HELP = "사용법: /quote <종목명>"
TREND_COMMAND_HELP = "사용법: /trend <종목명>"
TRADE_COMMAND_HELP = "\n".join(
    [
        "매매 주문 입력 안내:",
        f"{BUY_COMMAND_HELP}  예: /buy 삼성전자 1",
        f"{SELL_COMMAND_HELP}  예: /sell NAVER 1 200000",
        "주문은 실제 제출 전 반드시 확정이 필요합니다.",
    ]
)
LOOKUP_COMMAND_HELP = "\n".join(
    [
        "조회 입력 안내:",
        f"{QUOTE_COMMAND_HELP}  예: /quote 삼성전자",
        f"{TREND_COMMAND_HELP}  예: /trend 삼성전자",
    ]
)

# ---- 추론 과정 표시 (#260) ----

NAT_PROGRESS_MESSAGE = "⏳ 분석 중입니다..."
# 진행 메시지 삭제가 거부됐을 때 남길 종료 표시. "분석 중"이 영원히 남지 않게 한다.
PROGRESS_DONE_MESSAGE = "✅ 분석 완료"
