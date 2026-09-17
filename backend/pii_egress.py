"""외부 LLM 전송 경계 — 출처로 공개/개인을 가르고, 개인만 가리고, 실패하면 보내지 않는다 (#395).

#230은 `llm_chat`에서 프롬프트 **전체**에 `mask_pii`를 걸었다. 그런데 정규식은 값의
모양만 보므로 "평가금액 1,210,000원"(내 계좌)과 "매출 79조987억원"(DART 공시)을 가르지
못한다. 그 결과 실적 분석(`/earnings`)에 실적 수치가 자리표시자로 나가 분석 목적 자체가
훼손됐다. 이 구분은 인식기를 바꿔서(Presidio NER 포함) 풀리는 문제가 아니다 — **그 값이
누구의 것인지는 데이터를 가져온 곳만 안다.** 그래서 프롬프트를 조립하는 호출부가 출처를
표시하고, 이 모듈이 그 표시대로 마스킹한다.

## 공개/개인 구분 기준

**공개(public)** — 누구나 같은 값을 조회할 수 있고 이 사용자·이 계좌와 무관한 데이터.
:func:`public` 으로 감싼다. 지금 감싸는 출처는 다음이 전부다:

- 뉴스 (`mcp-news` `get_market_news`)
- 공시·실적 (`mcp-dart` `get_disclosure_signal`, `get_earnings_report`)
- 종목 단위 투자자 수급 (`mcp-trading` `get_investor_trading` — 시장 전체의 외국인·기관
  순매수이지 이 계좌의 체결이 아니다)
- 코드에 박힌 지시 문구 중 호출부가 공개로 표시한 것 (`order_rules.build_trigger_signal`)

**개인(personal)** — 이 사용자·이 계좌에서 나온 값, 또는 **출처를 확정할 수 없는 값.**

- 계좌번호, 예수금·주문가능금액, 평가금액·평가손익·매입가, 보유수량 (`get_balance` 리포트)
- 사용자가 입력한 모든 텍스트 (텔레그램 원문, 명령 인자로 받은 종목명·기간)

**표시하지 않은 문자열은 개인이다.** `llm_chat("nat", "...")` 처럼 그냥 넘긴 문자열은
종전(#230)과 똑같이 전체 마스킹된다. 호출부가 표시를 빠뜨리면 공개 수치가 과다 마스킹되는
**품질 저하**로 무너지지, 개인 수치가 새는 **유출**로 무너지지 않는다. 반대 기본값(표시
없으면 공개)은 새 호출부 하나가 조용히 잔고를 흘리는 설계다.

시세(현재가·호가)도 공개 데이터지만 backend가 시세를 프롬프트에 직접 싣는 경로는 지금 없다.
NAT 안에서 KIS 도구로 조회하는 시세는 잔고와 같은 pass-through 도구에서 나와 도구 단위로
마스킹된다(`finus_nat/.../pii_guard.MASKED_TOOLS`, #231) — 이 모듈의 범위 밖이며 남는 한계로
`docs/nfr-05-pii-masking.md`에 적었다.

## 공개 구간에도 거는 것 — 설정된 계좌번호

`KIS_ACCOUNT_NO`는 이 프로세스가 **값 자체를 아는** 유일한 개인 데이터다. 그래서 모양
추측이 아니라 값 일치로, 공개 구간까지 포함한 최종 프롬프트 전체에서 가린다. 두 가지를
닫는다:

- 정규식이 못 잡는 표기 — `_ACCOUNT_RE`는 10자리(CANO 8 + 상품코드 2)만 본다. "계좌
  12345678"처럼 CANO만 적으면 개인 구간에서도 평문으로 나간다.
- 호출부가 개인 데이터를 공개로 잘못 표시한 경우 — 적어도 계좌번호는 새지 않는다.

뉴스 본문에 이 사용자의 계좌번호와 자릿수까지 같은 숫자가 우연히 나올 확률은 무시할 만하고,
나오더라도 자리표시자로 바뀌어 왕복 복원될 뿐이다(과탐 = 품질 문제, 유출 아님).

## 실패하면 보내지 않는다 (fail-safe = 전송 차단)

마스킹 단계에서 무엇이든 실패하면 :class:`EgressBlocked`를 던지고 provider를 부르지 않는다.
원문으로 대신 보내는 갈래는 **두지 않는다.**

- 이 계층의 존재 이유가 개인정보를 외부 사업자에게 넘기지 않는 것이다. 실패 시 원문을 보내면
  계층이 가장 필요할 때(예상 못 한 입력) 스스로 꺼지는 설계가 된다.
- 차단의 비용은 **관측 가능한 실패 한 건**이다. 호출부는 이미 LLM 실패를 다룬다 — 텔레그램은
  "응답 생성 실패"를 보내고, 채점(`score_signal`)은 fail-open으로 넘기고, 분석 API는 오류를
  돌려준다. 유출은 되돌릴 수 없고 실패는 재시도하면 된다.
- 마스킹은 순수 문자열 연산이라 정상 입력에서는 실패하지 않는다. 차단이 실제로 발동하는 것은
  코드 결함(정규식 회귀, 잘못된 타입)이나 자리표시자 충돌뿐이고, 그 경우를 원문 전송으로
  덮으면 결함이 유출로 조용히 바뀐다.

차단 사유(`detail`)에는 프롬프트 내용을 싣지 않는다. 오류는 텔레그램 메시지·API 응답으로
나가기 때문이다. 원인은 서버 로그(`logger.exception`)에만 남긴다.

## 마스킹 전후 비교 로그

`backend.pii_egress` 로거의 DEBUG 레벨로 전송마다 마스킹 전·후 프롬프트와 요약(구간 수,
종류별 자리표시자 수)을 남긴다. backend는 INFO로 기동하므로(`main.py`의 `basicConfig`)
기본으로는 남지 않고, `PII_EGRESS_DEBUG_LOG=true`로 이 로거만 DEBUG로 올린다.

**켜면 평문 계좌 정보가 로그에 남는다.** 마스킹 전 프롬프트가 곧 이 계층이 외부로 내보내지
않으려는 값이다. 로컬 개발에서 마스킹 결과를 확인할 때만 켜고, 로그를 수집·전송하는 배포에서는
켜지 않는다.
"""
from __future__ import annotations

import logging
import os
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Union

from fastapi import HTTPException

from .config import PII_EGRESS_DEBUG_LOG
from .pii_mask import _Counter, mask_pii

logger = logging.getLogger(__name__)
if PII_EGRESS_DEBUG_LOG:
    # 이 로거만 올린다. basicConfig 핸들러는 레벨이 NOTSET이라 전파된 DEBUG 레코드를 그대로
    # 찍고, 다른 모듈의 DEBUG(httpx 등)는 계속 INFO로 걸러진다.
    logger.setLevel(logging.DEBUG)


@dataclass(frozen=True, slots=True)
class Segment:
    """프롬프트 한 구간. ``public=True``인 구간만 마스킹을 건너뛴다."""

    text: str
    public: bool = False


def personal(text: str) -> Segment:
    """이 사용자·계좌의 데이터이거나 출처를 확정할 수 없는 텍스트 — 마스킹한다."""
    return Segment(text, public=False)


def public(text: str) -> Segment:
    """뉴스·공시·실적·종목 수급처럼 누구나 같은 값을 조회하는 데이터 — 마스킹하지 않는다.

    모듈 docstring의 "공개/개인 구분 기준"에 든 출처만 감싼다. 판단이 서지 않으면 감싸지 않는다.
    """
    return Segment(text, public=True)


# 호출부가 넘기는 모양. 문자열 하나는 통째로 개인 구간이다. 시퀀스 안의 맨 문자열도 개인 구간이다
# — 정적 지시 문구를 일일이 personal()로 감싸지 않아도 되게 하되, 기본값은 여전히 마스킹이다.
EgressPrompt = Union[str, Sequence[Union[str, Segment]]]


class EgressBlocked(HTTPException):
    """마스킹을 끝내지 못해 외부 전송을 막았다. provider는 호출되지 않았다."""

    def __init__(self) -> None:
        super().__init__(
            status_code=503,
            detail="개인정보 비식별화에 실패해 외부 LLM 호출을 차단했습니다.",
        )


def render_unmasked(prompt: EgressPrompt) -> str:
    """구간을 이어 붙인 **마스킹 전** 원문. 테스트와 비교 로그용이다 — 외부로 보내지 않는다."""
    return "".join(segment.text for segment in _segments(prompt))


def prepare_egress(prompt: EgressPrompt, *, label: str) -> tuple[str, dict[str, str]]:
    """*prompt*를 외부로 보낼 문자열과 역치환 매핑으로 만든다.

    개인 구간은 `mask_pii`로, 설정된 계좌번호는 구간과 무관하게 가린다. 어느 단계에서든
    실패하면 :class:`EgressBlocked`를 던진다 — 호출자는 provider를 부르기 **전에** 이 함수를
    불러야 한다. *label*은 비교 로그에서 경로를 가르는 이름이다(provider 이름 등).
    """
    try:
        segments = _segments(prompt)
        parts: list[str] = []
        mapping: dict[str, str] = {}
        for segment in segments:
            if segment.public:
                parts.append(segment.text)
                continue
            masked, segment_mapping = _mask_without_collision(segment.text, mapping)
            parts.append(masked)
            mapping.update(segment_mapping)
        outgoing = _mask_configured_account("".join(parts), mapping)
    except EgressBlocked:
        raise
    except Exception as exc:
        logger.exception("외부 LLM 전송 전 비식별화 실패 — 전송을 차단합니다 (%s)", label)
        raise EgressBlocked() from exc

    if logger.isEnabledFor(logging.DEBUG):
        _log_comparison(label, segments, outgoing, mapping)
    return outgoing, mapping


def _segments(prompt: EgressPrompt) -> list[Segment]:
    if isinstance(prompt, str):
        return [personal(prompt)]
    segments: list[Segment] = []
    for item in prompt:
        if isinstance(item, Segment):
            segments.append(item)
        elif isinstance(item, str):
            segments.append(personal(item))
        else:
            # 무엇인지 모르는 값을 str()로 뭉개 보내지 않는다. 호출부 결함이다.
            raise TypeError(f"프롬프트 구간은 str 또는 Segment여야 합니다: {type(item).__name__}")
    return segments


# 구간마다 mask_pii를 따로 부르므로 구간마다 scope가 따로 뽑힌다. 두 구간이 같은 scope를 뽑으면
# 번호가 1부터 다시 시작해 자리표시자 문자열까지 같아지고(`<AMOUNT_abc123_1>`), 매핑을 합치는
# 순간 한쪽 원값이 다른 쪽으로 조용히 덮인다 — 응답에서 **남의 금액으로** 복원되는 오답이다.
# 확률(16^6)이 아니라 코드로 막는다(`pii_registry.active_mapping`과 같은 판단). 겹치면 그 구간을
# 다시 뽑고, 그래도 겹치면 차단한다 — 연달아 겹친다면 scope 생성이 고장 난 것이다.
_MAX_SCOPE_DRAWS = 3


def _mask_without_collision(
    text: str, taken: dict[str, str]
) -> tuple[str, dict[str, str]]:
    for _ in range(_MAX_SCOPE_DRAWS):
        masked, segment_mapping = mask_pii(text)
        if taken.keys().isdisjoint(segment_mapping):
            return masked, segment_mapping
    logger.error("프롬프트 구간 사이 자리표시자 scope가 반복해서 겹쳤습니다 — 전송을 차단합니다.")
    raise EgressBlocked()


def _configured_account_re() -> re.Pattern[str] | None:
    """`KIS_ACCOUNT_NO`에서 CANO(8자리)와 상품코드(2자리)를 뽑아 일치 정규식을 만든다.

    호출마다 env를 읽는다. 모듈 로드 시점에 굳히면 테스트가 env를 바꿔 이 경로를 검증할 수 없다.
    형식이 맞지 않으면(미설정·자릿수 불일치) 아는 값이 없는 것이므로 None이다 — 이 단계는
    정규식 마스킹 위에 얹는 보강이지 그것을 대신하지 않는다.
    """
    digits = re.sub(r"[\s-]", "", os.environ.get("KIS_ACCOUNT_NO", ""))
    if not re.fullmatch(r"\d{10}", digits):
        return None
    cano, product = digits[:8], digits[8:]
    # CANO 단독, CANO+상품코드(하이픈 유무). 앞뒤로 숫자가 이어지면 다른 수다.
    return re.compile(rf"(?<!\d){cano}(?:-?{product})?(?!\d)")


def _mask_configured_account(text: str, mapping: dict[str, str]) -> str:
    pattern = _configured_account_re()
    if pattern is None or pattern.search(text) is None:
        return text
    for _ in range(_MAX_SCOPE_DRAWS):
        counter = _Counter()
        found: dict[str, str] = {}

        def _replace(match: re.Match[str]) -> str:
            original = match.group(0)
            for placeholder, value in found.items():
                if value == original:
                    return placeholder
            placeholder = counter.next_placeholder("ACCOUNT")
            found[placeholder] = original
            return placeholder

        masked = pattern.sub(_replace, text)
        if mapping.keys().isdisjoint(found):
            mapping.update(found)
            return masked
    logger.error("계좌번호 자리표시자 scope가 반복해서 겹쳤습니다 — 전송을 차단합니다.")
    raise EgressBlocked()


def _log_comparison(
    label: str, segments: Sequence[Segment], outgoing: str, mapping: dict[str, str]
) -> None:
    kinds: dict[str, int] = {}
    for placeholder in mapping:
        kind = placeholder[1:].split("_", 1)[0]
        kinds[kind] = kinds.get(kind, 0) + 1
    logger.debug(
        "외부 LLM 전송 비식별화 [%s] 구간 %d개(공개 %d), 자리표시자 %s",
        label,
        len(segments),
        sum(1 for segment in segments if segment.public),
        kinds or "없음",
    )
    logger.debug("외부 LLM 전송 [%s] 마스킹 전:\n%s", label, render_unmasked(segments))
    logger.debug("외부 LLM 전송 [%s] 마스킹 후:\n%s", label, outgoing)
