"""외부 LLM 호출 직전 PII 마스킹 계층 (#230, F-17 / NFR-05).

`backend/services.py`의 `llm_chat()`이 4개 provider 경로(OpenAI/Anthropic/Ollama/NAT)의
단일 진입점이므로, 마스킹은 거기서 이 모듈의 `mask_pii`/`unmask_pii`를 호출하는
방식으로 끼운다. 이 모듈 자체는 provider를 모르고 순수 텍스트 변환만 담당한다.

## 방식: (a) 자리표시자 + 매핑 복원

    보내는 프롬프트: "<ACCOUNT_1> 계좌, 삼성전자 <QTY_1>주, 평가금액 <AMOUNT_1>, 총자산 <AMOUNT_2>"
    응답 수신 후   : <AMOUNT_1> -> 12,345,000원 로 역치환

상대 비교(AMOUNT_2 > AMOUNT_1처럼 "총자산이 평가금액보다 큰가")는 자리표시자만으로도
LLM이 수행할 수 있어 유지되지만, **절대 금액 기반 판단**("이 종목 비중이 1천만원으로
과도하다")은 원값이 프롬프트에 없으므로 LLM이 할 수 없다. 이것은 이 설계의 알려진 한계이며,
정밀한 절대 판단이 필요해지면 (b) 비율·구간 변환(예: "포트폴리오 대비 12%")을 별도 이슈로
검토해야 한다 — 변환 규칙·구간 경계 설계 비용이 이 이슈 범위를 넘어 채택하지 않았다.

## 왜 정규식이고 Presidio가 아닌가 (#230 착수 시 판단, 이슈 코멘트에도 근거를 남김)

대상 3종(한국 계좌번호/원화 금액/보유 수량)은 모두 정규식으로 충분히 식별 가능한
구조화된 패턴이다. `_build_toolless_prompt`/`_build_nat_prompt`/`generate_morning_briefing`이
조립하는 프롬프트는 종목명 + 트리거 컨텍스트 + KIS 잔고 텍스트 + 고정 지시문뿐이며, 사용자
자유 입력이 섞이지 않으므로 한국어 인명 등 NER이 필요한 대상이 들어올 여지가 없다.
presidio-analyzer는 spaCy NER 모델 다운로드가 Docker 이미지·CI 시간에 얹히는 비용이 있어,
이 범위에서는 이점 없이 비용만 발생한다.

## 미적용 경로 (이 모듈이 막지 못하는 것)

- NAT 멀티에이전트가 자체적으로 MCP 도구(KIS 잔고 등)를 호출해 만드는 프롬프트 조각은
  backend가 보지 못하므로 이 계층 밖이다 (#231에서 다룬다).
- Telegram 명령 경로는 애초에 이 LLM 호출 경로를 타지 않는다 (#232에서 다룬다).
- 정규식 기반이므로 위 3종 패턴에서 벗어난 표기(예: 계좌번호를 자릿수가 다른 증권사
  형식으로 표기)는 놓칠 수 있다. `_ACCOUNT_RE`는 KIS_ACCOUNT_NO 형식(CANO 8자리 +
  상품코드 2자리, 총 10자리, 하이픈 유무 무관)만 다룬다(mcp-trading/index.js:96,
  mcp-trading/balance.js:23-26 기준).
"""
from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────────
# 커스텀 recognizer 3종
# ──────────────────────────────────────────────────────────────────────────

# 한국 계좌번호 — KIS_ACCOUNT_NO 형식: CANO(8자리) + ACNT_PRDT_CD(2자리) = 10자리,
# 하이픈 유무 모두 허용한다(.env.example: KIS_ACCOUNT_NO=1234567801,
# 사람이 읽는 표기는 보통 "12345678-01"). 앞뒤로 숫자가 더 이어지면(전화번호·긴 ID 등과의
# 오탐 방지) 매치하지 않는다.
_ACCOUNT_RE = re.compile(r"(?<!\d)(\d{8})-?(\d{2})(?!\d)")

# 원화 금액 — "1,234,567원" / "1234567원" (콤마 없이 원만 붙는 경우) 모두 커버한다.
# 콤마가 있으면 3자리씩 끊긴 형태만 인정해(오탐 방지) 임의의 콤마 위치는 매치하지 않는다.
# 선두 \d+는 콤마를 만나면 자연히 멈추므로 "1,234,567"처럼 정상적으로 3자리씩
# 끊긴 표기와 "1234567"처럼 끊기지 않은 표기를 하나의 패턴으로 함께 다룰 수 있다.
_AMOUNT_WON_RE = re.compile(r"(?<!\d)\d+(?:,\d{3})*(?:\.\d+)?원")

# "123만원" / "1.5억원" 처럼 만/억 단위가 붙는 표기.
_AMOUNT_UNIT_RE = re.compile(r"(?<!\d)\d+(?:\.\d+)?(?:만|억)\s?원")

# 금액 라벨(잔고 리포트·프롬프트에서 실제로 쓰이는 어휘) 바로 뒤에 "원" 접미사 없이
# 나오는 숫자 — 원화 금액 표기 편차("1234567"처럼 단위 없이 그대로 나오는 값)를 위해
# 라벨 컨텍스트로 한정한다. 라벨 없는 bare 숫자는 종목코드·날짜 등과 구분할 수 없어
# 일부러 매치하지 않는다(과탐 방지가 F-17 취지보다 우선하지 않도록, 실제 마스킹
# 대상은 KIS 잔고 리포트 어휘에 고정한다). 콤마 형식(1,234,567)이거나, 콤마 없이도
# 4자리 이상 이어지는 숫자(1234567)만 금액으로 인정한다 — 1~3자리 bare 숫자까지
# 잡으면 라벨 인근의 사소한 숫자(예: 순번)까지 마스킹 대상이 되어 과탐이 커진다.
_LABELED_AMOUNT_RE = re.compile(
    r"(?:금액|잔고|자산|예수금|손익|투자금)\s*[:：]?\s*"
    r"(?P<amount>(?<!\d)(?:\d{1,3}(?:,\d{3})+|\d{4,})(?!\d))"
)

# 보유 수량 — 잔고 리포트의 "{qty}주" 표기(mcp-trading/balance.js formatQuantity 결과).
# "3주일"(3 weeks)처럼 기간을 세는 표기와의 오탐을 막기 위해 뒤에 "일"이 오지 않는
# 경우만 매치한다. 자리표시자는 숫자만 대체하고 "주"는 원문 그대로 남긴다 — 이 모듈의
# 다른 recognizer(금액)와 달리 단위 문자를 프롬프트에 남겨 LLM이 "이 값은 수량"이라는
# 문맥을 잃지 않게 한다.
_QTY_RE = re.compile(r"(?<!\d)\d+(?=주(?!일))")

_PLACEHOLDER_RE = re.compile(r"<(ACCOUNT|AMOUNT|QTY)_(\d+)>")


class _Counter:
    """마스킹 1회 호출 동안만 사는 타입별 자리표시자 카운터.

    llm_chat() 호출마다 새로 만들어 반환하는 mapping과 함께 지역 변수로만 존재한다.
    모듈 전역에 두면 동시 요청(asyncio.gather로 병렬 호출되는 llm_chat들)이 서로의
    카운터를 공유해 자리표시자 번호가 요청을 넘나들며 섞인다 — 그 자체로 매핑 오염은
    아니지만 굳이 그런 결합을 만들 이유가 없다. mapping은 항상 함수 지역 dict다.
    """

    def __init__(self) -> None:
        self.values: dict[str, int] = {"ACCOUNT": 0, "AMOUNT": 0, "QTY": 0}

    def next_placeholder(self, kind: str) -> str:
        self.values[kind] += 1
        return f"<{kind}_{self.values[kind]}>"


def mask_pii(text: str) -> tuple[str, dict[str, str]]:
    """계좌번호·원화 금액·보유 수량을 자리표시자로 치환한다.

    반환된 mapping은 이 호출 전용이다 — 호출자(services.llm_chat)가 요청 스코프
    지역 변수로만 들고 있어야 하며, 모듈 전역에 저장하면 동시 요청의 매핑이
    서로 덮어써진다.

    text가 비어 있으면(None/빈 문자열) 아무것도 마스킹하지 않고 그대로 반환한다 —
    LLM에 보낼 내용이 없으니 마스킹할 것도 없다.
    """
    if not text:
        return text, {}

    counter = _Counter()
    mapping: dict[str, str] = {}

    def _replace(kind: str, match: re.Match[str]) -> str:
        original = match.group(0)
        placeholder = counter.next_placeholder(kind)
        mapping[placeholder] = original
        return placeholder

    def _replace_labeled_amount(match: re.Match[str]) -> str:
        original = match.group("amount")
        placeholder = counter.next_placeholder("AMOUNT")
        mapping[placeholder] = original
        # 라벨 텍스트는 그대로 두고 숫자 부분만 자리표시자로 바꾼다.
        return match.group(0)[: match.start("amount") - match.start(0)] + placeholder

    def _replace_qty(match: re.Match[str]) -> str:
        original = match.group(0)
        placeholder = counter.next_placeholder("QTY")
        mapping[placeholder] = original
        return placeholder

    # 순서: 계좌번호(가장 구체적인 10자리 패턴) -> 금액(원/만원/억원 접미사, 그다음
    # 라벨 기반 bare 숫자) -> 수량. 앞 단계에서 자리표시자로 치환된 구간은 숫자가
    # 아니므로 뒤 단계 정규식이 다시 건드리지 않는다.
    masked = _ACCOUNT_RE.sub(lambda m: _replace("ACCOUNT", m), text)
    masked = _AMOUNT_WON_RE.sub(lambda m: _replace("AMOUNT", m), masked)
    masked = _AMOUNT_UNIT_RE.sub(lambda m: _replace("AMOUNT", m), masked)
    masked = _LABELED_AMOUNT_RE.sub(_replace_labeled_amount, masked)
    masked = _QTY_RE.sub(_replace_qty, masked)

    return masked, mapping


def unmask_pii(text: str, mapping: dict[str, str]) -> str:
    """자리표시자를 원값으로 역치환한다.

    LLM 응답에서 자리표시자가 변형되거나(예: <AMOUNT_1> -> <AMOUNT1>) 존재하지 않는
    자리표시자가 지어질 수 있다(예: <AMOUNT_9>). 이 함수는 어떤 경우에도 예외를
    던지지 않는다 — 역치환 실패가 분석 결과 전체를 죽이면 안 되므로, 실패한
    자리표시자는 원문 그대로 남기고 경고 로그만 남긴다(fail-open).
    """
    if not text or not mapping:
        return text

    missing: list[str] = []

    def _restore(match: re.Match[str]) -> str:
        placeholder = match.group(0)
        value = mapping.get(placeholder)
        if value is None:
            missing.append(placeholder)
            return placeholder
        return value

    try:
        restored = _PLACEHOLDER_RE.sub(_restore, text)
    except Exception:
        # 정규식 치환 자체가 실패하는 경우는 사실상 없지만(순수 문자열 연산),
        # 방어적으로 원문을 그대로 반환한다 — 역치환이 분석을 막아서는 안 된다.
        logger.exception("PII 자리표시자 역치환 중 예상치 못한 오류가 발생했습니다.")
        return text

    if missing:
        logger.warning(
            "LLM 응답에서 매핑에 없는 PII 자리표시자를 발견해 원문 그대로 남겼습니다: %s",
            missing,
        )

    return restored
