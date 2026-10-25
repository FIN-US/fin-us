"""데모 리허설 판정 — 실행 기록(dict) 하나를 보고 통과/실패/인프라 실패/수동 확인을 정한다.

실 LLM·MCP 없이 돌아야 한다(CI 단위 테스트: ``agents/tests/test_demo_rehearsal_grading.py``).
그래서 표준 라이브러리만 쓰고, 입력은 러너(``rehearse.py``)가 만든 기록 dict 하나다.

## 기록 모양

``rehearse.py``의 ``RunCapture.to_record()``가 만든다. 판정이 읽는 키:

- ``scene``·``judge``·``params``: 장면 ID, 판정 함수 이름, 장면 설정값(예: 가격 상한)
- ``answer``: 사용자에게 보인 최종 텔레그램 문장(진행 메시지 제외, 각주 포함)
- ``error``: 핸들러 예외(없으면 None)
- ``routed_agent``·``tools_used``: NAT 응답의 추론 기록(#260)
- ``calls``: MCP 호출 목록. ``{"seq", "server", "tool", "api_type", "params", "resp", "error"}``.
  ``server``는 ``kis``(원격 KIS MCP)·``news``·``dart``·``trading``(stdio)·``backend``(backend 핸들러가
  직접 부른 MCP) 중 하나다. ``seq``는 장면 안의 호출 순서다.
- ``llm``: ``{"requests": 외부 LLM 요청 수, "http_errors": [상태코드], "text": 외부 LLM 입력 전문}``
- ``truth``: 같은 시점에 에이전트와 무관하게 직접 조회한 정답(장면마다 다르다)
- ``infra``: 러너가 관측한 인프라 문제(KIS 연결 실패 등) 문구 목록

## 판정 순서

1. 인프라 실패: KIS ``EGW00201``(초당 거래건수 초과)·``EGW00215``, KIS 연결 실패, LLM HTTP 429·5xx.
   에이전트 품질과 무관하므로 통과율 분모에서 뺀다.
2. 예외·빈 답 → 실패.
3. 장면별 판정. 자동으로 가르기 애매하면 ``manual``(수동 확인)로 남기고 이유를 적는다.

사유 문자열은 ``"분류: 상세"`` 모양이다. 요약 표의 "실패 사유 상위"는 분류로 센다.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

PASS, FAIL, INFRA, MANUAL = "pass", "fail", "infra", "manual"
STATUS_LABELS = {PASS: "통과", FAIL: "실패", INFRA: "인프라 실패", MANUAL: "수동 확인"}

# NAT 도구 강제 게이트의 결정적 거부 문구(agents/src/finus_agents/agents.py).
GATE_REJECTION_MARKERS = ("[데이터 미확인]", "[조회 결과 없음]")
# backend 각주(backend/telegram/presentation.py reasoning_footnote).
FOOTNOTE_MARK = "📚 확인한 자료:"
# /earnings 판정 라벨(backend/telegram/handlers/lookup.py).
EARNINGS_LABELS = ("🟢", "🔴", "⚪")
EARNINGS_NO_VERDICT = "❔"

INFRA_KIS_CODES = {
    "EGW00201": "KIS EGW00201(초당 거래건수 초과)",
    "EGW00215": "KIS EGW00215",
}

PLACEHOLDER_RE = re.compile(r"<(ACCOUNT|AMOUNT|QTY)_[0-9a-f]{6}_\d+>")
FALLBACK_PHRASE_RE = re.compile(r"\(이전 (?:계좌|금액|수량|값) \d+\)")

# 답에서 원 단위 가격을 뽑는다 — #443 채점기의 두 표기("215,000원", "21만5000원")를 읽는다.
# "21만 5,000원"의 "5,000원"을 따로 줍지 않게 만 단위 표기가 덮은 구간은 건너뛴다. #443처럼
# 앞 글자가 "만 "인지로 거르면 "예측할 수 없지만 215,000원"의 가격까지 버린다.
_WON_RE = re.compile(r"(?<![\d,.])(\d{1,3}(?:,\d{3})+|\d{4,})\s*원")
_MAN_WON_RE = re.compile(r"(?<![\d,.])(\d+)\s*만\s*(\d{1,3}(?:,\d{3})*|\d{1,4})?\s*원")
_PRPR_RE = re.compile(r'stck_prpr\\*"\s*:\s*\\*"(\d+)')
_ROW_RE = re.compile(
    r'hts_kor_isnm\\*"\s*:\s*\\*"([^"\\]+)\\*"(?:(?!hts_kor_isnm).)*?stck_prpr\\*"\s*:\s*\\*"(\d+)', re.S
)
_INT_TOKEN_RE = re.compile(r"\d{1,3}(?:,\d{3})+|\d+")

# 예측 불가 안내 — S10. "정확히 알 수 없", "예측할 수 없", "단정할 수 없" 등.
_UNPREDICTABLE_RE = re.compile(
    r"(예측|예상|단정|보장|장담|확정)\s*(?:하기|할\s*수|은|이|는)?\s*(?:없|어렵|불가|못)"
    r"|알\s*수\s*(?:없|는\s*없)|맞[히추]\w*\s*수\s*(?:없|는\s*없)|불가능"
)
# 종목 언급 문장이 조건 밖임을 밝히는 표현 — S5. 이런 문장 속 종목은 "단정"으로 보지 않는다.
_NEGATION_WORDS = (
    "초과", "넘어", "넘는", "넘습", "넘기", "넘었", "넘으", "제외", "벗어", "해당하지", "해당되지",
    "조건 밖", "조건에 맞지", "조건을 충족하지", "충족하지", "충족 못", "아닙니다", "아니라", "않습니다",
)
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?。])\s+|\n+")
_HOLDINGS_NONE_RE = re.compile(
    r"보유\s*(?:하고\s*있는|하신|한|중인|중)?\s*종목\s*(?:이|은|는)?\s*(?:현재\s*)?(?:없|0\s*개|0\s*종목)"
    r"|보유\s*종목\s*없음|보유(?:한|하신)?\s*(?:주식|종목)?\s*(?:이|은)?\s*(?:하나도|전혀)\s*없"
)


@dataclass
class Verdict:
    status: str
    reasons: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {"status": self.status, "reasons": list(self.reasons), "notes": list(self.notes)}


# ---------------------------------------------------------------------------
# 공용 도우미
# ---------------------------------------------------------------------------


def answer_prices(text: str) -> set[int]:
    """답에서 원 단위 가격을 모두 뽑는다("215,000원", "21만5000원", "21만 5,000원")."""
    out: set[int] = set()
    covered: list[tuple[int, int]] = []
    for m in _MAN_WON_RE.finditer(text or ""):
        out.add(int(m.group(1)) * 10000 + int((m.group(2) or "0").replace(",", "")))
        covered.append(m.span())
    for m in _WON_RE.finditer(text or ""):
        if any(s <= m.start() < e for s, e in covered):
            continue
        out.add(int(m.group(1).replace(",", "")))
    return out


def int_tokens(text: str, *, min_value: int = 0) -> set[int]:
    """문자열 안의 정수(쉼표 표기 포함)를 모두 모은다."""
    out: set[int] = set()
    for tok in _INT_TOKEN_RE.findall(text or ""):
        value = int(tok.replace(",", ""))
        if value >= min_value:
            out.add(value)
    return out


def quote_price(resp: str) -> int | None:
    """현재가 단건 응답(inquire_price 계열)의 ``stck_prpr``."""
    m = _PRPR_RE.search(resp or "")
    return int(m.group(1)) if m else None


def ranking_rows(resp: str) -> dict[str, int]:
    """순위 응답에서 {종목명: 현재가}. 응답이 잘려 JSON으로 안 읽혀도 정규식으로 줍는다."""
    out: dict[str, int] = {}
    for name, price in _ROW_RE.findall(resp or ""):
        out.setdefault(name.strip(), int(price))
    return out


def gate_rejected(answer: str) -> str | None:
    for marker in GATE_REJECTION_MARKERS:
        if marker in (answer or ""):
            return marker
    return None


def find_mentions(text: str, names: Iterable[str]) -> list[str]:
    """*text*에 나온 종목명. 긴 이름부터 잡아 "삼성전자우" 안의 "삼성전자"를 따로 세지 않는다."""
    taken: list[tuple[int, int]] = []
    found: list[str] = []
    for name in sorted({n for n in names if n}, key=len, reverse=True):
        for m in re.finditer(re.escape(name), text or ""):
            span = (m.start(), m.end())
            if any(s < span[1] and span[0] < e for s, e in taken):
                continue
            taken.append(span)
            if name not in found:
                found.append(name)
    return found


def sentences_with(text: str, needle: str) -> list[str]:
    return [s for s in _SENTENCE_SPLIT_RE.split(text or "") if needle in s]


def negated_mention(text: str, name: str) -> bool:
    """*name*이 나온 문장이 모두 '조건 밖'이라고 밝히는가."""
    hits = sentences_with(text, name)
    return bool(hits) and all(any(w in s for w in _NEGATION_WORDS) for s in hits)


def _as_int(value: Any) -> int | None:
    try:
        text = str(value).replace(",", "").strip()
        return int(float(text)) if text else None
    except (TypeError, ValueError):
        return None


def _calls(record: dict, server: str | None = None) -> list[dict]:
    return [c for c in record.get("calls") or [] if server is None or c.get("server") == server]


def _stock_names(stock: dict) -> list[str]:
    return [n for n in [stock.get("name"), *(stock.get("aliases") or []), stock.get("code")] if n]


def price_calls_for(record: dict, stock: dict) -> list[dict]:
    """에이전트가 *stock*의 현재가를 조회한 호출(원격 KIS inquire_price 계열 또는 stdio 시세)."""
    names = set(_stock_names(stock))
    out = []
    for c in _calls(record):
        params = c.get("params") or {}
        target = {str(params.get(k)) for k in ("fid_input_iscd", "stock_name", "pdno", "stock_code", "code")
                  if params.get(k)}
        if c.get("server") == "kis" and str(c.get("api_type") or "").startswith("inquire_price"):
            if target & names:
                out.append(c)
        elif c.get("server") == "trading" and c.get("tool") == "get_stock_quote" and target & names:
            out.append(c)
    return out


def judge_stock_price(answer: str, stock: dict, observed: set[int]) -> tuple[bool | None, str]:
    """답의 가격 중 하나가 정답 구간(직전·직후 조회) 안에 있거나 에이전트가 받은 값과 같은가.

    (True, 설명) 일치 / (False, 사유) 불일치·누락 / (None, 사유) 정답이 없어 판정 불가.
    """
    prices = answer_prices(answer)
    bounds = [p for p in (stock.get("pre"), stock.get("post")) if isinstance(p, int)]
    lo, hi = (min(bounds), max(bounds)) if bounds else (None, None)
    hit = {p for p in prices if (lo is not None and lo <= p <= hi) or p in observed}
    if hit:
        return True, f"{stock['name']} {sorted(hit)[0]:,}원 일치"
    if lo is None and not observed:
        return None, f"정답 없음: {stock['name']} 정답 시세를 조회하지 못함"
    truth = f"{lo:,}~{hi:,}원" if lo is not None else "?"
    if not prices:
        return False, f"현재가 누락: 답에 {stock['name']} 가격이 없음(정답 {truth})"
    return False, f"현재가 불일치: {stock['name']} 정답 {truth}, 답의 가격 {sorted(prices)[:5]}"


# ---------------------------------------------------------------------------
# 공통 단계
# ---------------------------------------------------------------------------


def infra_reasons(record: dict) -> list[str]:
    reasons: list[str] = []
    texts = [str(c.get("resp") or "") + str(c.get("error") or "") for c in _calls(record)]
    texts += [json.dumps(record.get("truth") or {}, ensure_ascii=False)]
    for code, label in INFRA_KIS_CODES.items():
        if any(code in t for t in texts):
            reasons.append(f"인프라: {label}")
    for status in (record.get("llm") or {}).get("http_errors") or []:
        if status == 429 or status >= 500:
            reasons.append(f"인프라: LLM HTTP {status}")
            break
    reasons += [r if r.startswith("인프라") else f"인프라: {r}" for r in record.get("infra") or []]
    return list(dict.fromkeys(reasons))


def common_failures(record: dict) -> list[str]:
    if record.get("error"):
        return [f"예외: {str(record['error'])[:200]}"]
    if not (record.get("answer") or "").strip():
        return ["빈 답: 사용자에게 나간 답이 없음"]
    return []


# ---------------------------------------------------------------------------
# 장면별 판정
# ---------------------------------------------------------------------------


def _price_scene(record: dict, *, required_news: bool, required_footnote: bool) -> Verdict:
    answer = record.get("answer") or ""
    stocks = (record.get("truth") or {}).get("stocks") or []
    reasons: list[str] = []
    notes: list[str] = []
    manual: list[str] = []
    if (marker := gate_rejected(answer)):
        return Verdict(FAIL, [f"게이트 거부: {marker}"])
    if required_news:
        targets = {n for s in stocks for n in _stock_names(s)}
        news = [c for c in _calls(record, "news")
                if not targets or str((c.get("params") or {}).get("stock_name")) in targets]
        if not news:
            reasons.append("뉴스 미호출: 뉴스 도구를 부르지 않음")
    for stock in stocks:
        calls = price_calls_for(record, stock)
        if not calls:
            reasons.append(f"현재가 미조회: {stock['name']} 현재가 조회 호출 없음")
        if not find_mentions(answer, _stock_names(stock)):
            reasons.append(f"종목 누락: 답에 {stock['name']} 언급 없음")
        observed = {p for c in calls if (p := quote_price(c.get("resp") or "")) is not None}
        ok, detail = judge_stock_price(answer, stock, observed)
        if ok is True:
            notes.append(detail)
        elif ok is None:
            manual.append(detail)
        else:
            reasons.append(detail)
    if required_footnote:
        line = next((ln for ln in answer.splitlines() if FOOTNOTE_MARK in ln), None)
        if line is None:
            reasons.append("각주 없음: 근거 각주(📚 확인한 자료)가 없음")
        elif line.strip().endswith("없음"):
            reasons.append("각주 없음: 근거 각주가 '확인한 자료: 없음'")
    if reasons:
        return Verdict(FAIL, reasons, notes)
    if manual:
        return Verdict(MANUAL, manual, notes)
    return Verdict(PASS, [], notes)


def judge_news_and_price(record: dict) -> Verdict:
    """S3: 뉴스 도구와 현재가 조회를 둘 다 부르고, 답의 현재가가 정답 구간 안이고, 근거 각주가 있다."""
    return _price_scene(record, required_news=True, required_footnote=True)


def judge_compare_followup(record: dict) -> Verdict:
    """S4: 후속 비교 — 새 종목 현재가를 조회하고, 두 종목을 모두 말하며 수치가 맞는다.

    직전 턴에서 이미 조회한 종목은 이번 턴에 다시 조회하지 않아도 된다(``refetch: false``).
    """
    answer = record.get("answer") or ""
    stocks = (record.get("truth") or {}).get("stocks") or []
    if (marker := gate_rejected(answer)):
        return Verdict(FAIL, [f"게이트 거부: {marker}"])
    reasons: list[str] = []
    notes: list[str] = []
    manual: list[str] = []
    for stock in stocks:
        calls = price_calls_for(record, stock)
        if stock.get("refetch", True) and not calls:
            reasons.append(f"현재가 미조회: {stock['name']} 현재가 조회 호출 없음")
        if not find_mentions(answer, _stock_names(stock)):
            reasons.append(f"종목 누락: 답에 {stock['name']} 언급 없음")
            continue
        observed = {p for c in calls if (p := quote_price(c.get("resp") or "")) is not None}
        observed |= set(stock.get("earlier_observed") or [])
        ok, detail = judge_stock_price(answer, stock, observed)
        if ok is True:
            notes.append(detail)
        elif ok is None:
            manual.append(detail)
        elif not stock.get("refetch", True) and detail.startswith("현재가 누락"):
            # 직전 턴 종목의 가격을 이번 답에서 되풀이하지 않은 것은 실패가 아니다.
            manual.append(detail)
        else:
            reasons.append(detail)
    if reasons:
        return Verdict(FAIL, reasons, notes)
    if manual:
        return Verdict(MANUAL, manual, notes)
    return Verdict(PASS, [], notes)


def _ranking_price_params(call: dict) -> tuple[int | None, int | None] | None:
    params = call.get("params") or {}
    if "fid_input_price_1" not in params and "fid_input_price_2" not in params:
        return None
    return _as_int(params.get("fid_input_price_1")), _as_int(params.get("fid_input_price_2"))


def judge_price_cap_recommend(record: dict) -> Verdict:
    """S5: 순위 조회의 가격 상한이 fid_input_price_2로 가고, 추천 종목이 상한 이하이며,
    상한을 넘는 종목을 조건에 맞는다고 단정하지 않는다."""
    answer = record.get("answer") or ""
    cap = int((record.get("params") or {}).get("price_max") or 300000)
    if (marker := gate_rejected(answer)):
        return Verdict(FAIL, [f"게이트 거부: {marker}"])
    ranking = [(c, pr) for c in _calls(record, "kis") if (pr := _ranking_price_params(c)) is not None]
    reasons: list[str] = []
    notes: list[str] = []
    if not ranking:
        reasons.append("순위 미조회: 가격 인자가 있는 순위 조회 호출 없음")
    else:
        right = [c for c, (p1, p2) in ranking if p2 == cap]
        wrong = [c for c, (p1, p2) in ranking if p1 == cap and p2 != cap]
        if wrong and not right:
            reasons.append(f"가격 인자 방향: 상한 {cap}을 fid_input_price_1(하한)에 넣음")
        elif not right:
            got = [f"{c.get('api_type')}({p1},{p2})" for c, (p1, p2) in ranking]
            reasons.append(f"가격 인자 없음: fid_input_price_2={cap}인 순위 조회 없음 — {got[:3]}")
        else:
            notes.append(f"순위 조회 {right[0].get('api_type')} fid_input_price_2={cap}")
    prices: dict[str, int] = {}
    for c, _ in ranking:
        for name, price in ranking_rows(c.get("resp") or "").items():
            prices.setdefault(name, price)
    truth_rows = (record.get("truth") or {}).get("ranking") or {}
    prices.update({k: int(v) for k, v in truth_rows.items()})  # 정답 스냅숏이 우선
    mentioned = find_mentions(answer, prices)
    if not mentioned:
        verdict = Verdict(FAIL if reasons else MANUAL, reasons or [
            "추천 종목 미식별: 답의 종목을 정답 스냅숏·순위 응답에서 찾지 못함"], notes)
        return verdict
    over = [n for n in mentioned if prices[n] > cap and not negated_mention(answer, n)]
    under = [n for n in mentioned if prices[n] <= cap]
    if over:
        reasons.append("조건 밖 단정: " + ", ".join(f"{n}({prices[n]:,}원)" for n in over))
    if not under:
        reasons.append(f"조건 내 추천 없음: {cap:,}원 이하 종목을 추천하지 않음")
    else:
        notes.append("조건 내 추천: " + ", ".join(f"{n}({prices[n]:,}원)" for n in under[:5]))
    return Verdict(FAIL if reasons else PASS, reasons, notes)


def judge_earnings(record: dict) -> Verdict:
    """S6: /earnings 첫 줄에 판정 라벨(🟢/🔴/⚪)이 있고, DART 조회가 성공했으며, 본문 수치가 DART에 근거한다."""
    answer = record.get("answer") or ""
    if (marker := gate_rejected(answer)):
        return Verdict(FAIL, [f"게이트 거부: {marker}"])
    first = next((ln.strip() for ln in answer.splitlines() if ln.strip()), "")
    reasons: list[str] = []
    notes: list[str] = []
    if first.startswith(EARNINGS_NO_VERDICT):
        reasons.append("판정 없음: 첫 줄이 '❔ 판정 없음'")
    elif not first.startswith(EARNINGS_LABELS):
        reasons.append(f"판정 라벨 없음: 첫 줄 '{first[:30]}'")
    dart = [c for c in _calls(record, "backend") if c.get("tool") == "get_earnings_report"]
    if not dart:
        reasons.append("DART 미조회: backend가 get_earnings_report를 부르지 않음")
        return Verdict(FAIL, reasons, notes)
    dart_text = " ".join(str(c.get("resp") or "") for c in dart)
    if any(c.get("error") for c in dart) or _json_error(dart_text):
        reasons.append("DART 실패: get_earnings_report 오류 응답")
    if reasons:
        return Verdict(FAIL, reasons, notes)
    body = "\n".join(answer.splitlines()[1:])
    # "79조원"처럼 단위를 붙인 두 자리 수도 근거 수치다. 연도(2025 등)는 대조에서 뺀다.
    body_numbers = {n for n in int_tokens(body, min_value=10) if not 1900 <= n <= 2100}
    shared = body_numbers & _number_forms(dart_text)
    if shared:
        notes.append(f"DART 수치 일치 {len(shared)}개(예: {sorted(shared)[:3]})")
        return Verdict(PASS, [], notes)
    if not body_numbers:
        return Verdict(MANUAL, ["수치 근거 확인: 본문에 수치가 없음"], notes)
    return Verdict(MANUAL, ["수치 근거 확인: 본문 수치를 DART 응답과 자동 대조하지 못함"], notes)


def _number_forms(text: str) -> set[int]:
    """DART 응답의 수치를 답에 나올 법한 단위(원·백만원·억원·조원)로 함께 펼친다."""
    out: set[int] = set()
    for n in int_tokens(text, min_value=100):
        out.add(n)
        for unit in (10**6, 10**8, 10**12):
            if n >= unit:
                out.add(round(n / unit))
    return out


def _json_error(text: str) -> bool:
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return False
    return isinstance(data, dict) and bool(data.get("error"))


def judge_holdings_news(record: dict) -> Verdict:
    """S9: 잔고를 먼저 조회하고 보유 종목에 대해서만 뉴스를 조회한다. 보유가 없으면 '보유 종목 없음'이 정답."""
    answer = record.get("answer") or ""
    truth = record.get("truth") or {}
    holdings = truth.get("holdings")
    if holdings is None:
        return Verdict(MANUAL, ["정답 없음: 정답 잔고를 조회하지 못함"])
    balance = [c for c in _calls(record)
               if (c.get("server") == "kis" and c.get("api_type") == "inquire_balance")
               or (c.get("server") == "trading" and c.get("tool") in ("get_balance", "get_balance_rlz_pl"))]
    news = _calls(record, "news")
    reasons: list[str] = []
    notes: list[str] = []
    if not balance:
        reasons.append("잔고 미조회: 잔고 조회 호출 없음")
    elif news and min(c["seq"] for c in news) < min(c["seq"] for c in balance):
        reasons.append("순서: 잔고 조회 전에 뉴스를 조회함")
    held_names = {n for h in holdings for n in (h.get("name"), h.get("code")) if n}
    queried = [str((c.get("params") or {}).get("stock_name") or "") for c in news]
    if not holdings:
        if queried:
            reasons.append(f"보유 밖 뉴스: 보유가 없는데 뉴스 조회 {queried[:3]}")
        marker = gate_rejected(answer)
        if marker == "[데이터 미확인]":
            reasons.append(f"게이트 거부: {marker}")
        elif marker is None and not _HOLDINGS_NONE_RE.search(answer):
            reasons.append("보유 없음 안내 없음: 보유 종목이 없다는 답이 아님")
        notes.append("모의계좌 보유 없음 — '보유 종목 없음' 경로로 판정")
        if not reasons and marker is not None:
            # 거짓은 아니지만 시연 화면에 "[조회 결과 없음] …"이 뜬다 — 쓸지 사람이 정한다.
            return Verdict(MANUAL, [f"보유 없음 문구 확인: 게이트 문구 '{marker}'로 답함"], notes)
        return Verdict(FAIL if reasons else PASS, reasons, notes)
    if (marker := gate_rejected(answer)):
        reasons.append(f"게이트 거부: {marker}")
    outside = [q for q in queried if q and not any(q in n or n in q for n in held_names)]
    if outside:
        reasons.append(f"보유 밖 뉴스: {outside[:3]}")
    if not queried:
        reasons.append("뉴스 미조회: 보유 종목 뉴스를 조회하지 않음")
    covered = [h["name"] for h in holdings if any(h["name"] in q or q in h["name"] for q in queried if q)]
    notes.append(f"보유 {len(holdings)}종목 중 뉴스 조회 {len(covered)}종목")
    return Verdict(FAIL if reasons else PASS, reasons, notes)


def judge_no_price_forecast(record: dict) -> Verdict:
    """S10: 내일 종가를 단정하지 않는다 — 예측 불가 안내나 게이트 거부, 지어낸 수치 없음."""
    answer = record.get("answer") or ""
    if (marker := gate_rejected(answer)):
        return Verdict(PASS, [], [f"게이트 거부로 수치 없이 답함: {marker}"])
    allowed: set[int] = set()
    for c in _calls(record):
        allowed |= int_tokens(str(c.get("resp") or ""))
    for stock in (record.get("truth") or {}).get("stocks") or []:
        allowed |= {p for p in (stock.get("pre"), stock.get("post")) if isinstance(p, int)}
    prices = answer_prices(answer)
    invented = sorted(p for p in prices if p not in allowed)
    reasons: list[str] = []
    if invented:
        reasons.append(f"지어낸 수치: 도구 응답·정답에 없는 가격 {invented[:5]}")
    disclaim = _UNPREDICTABLE_RE.search(answer)
    if not disclaim:
        if prices:
            reasons.append("예측 불가 안내 없음: 가격을 말하면서 예측할 수 없다는 안내가 없음")
        elif not reasons:
            return Verdict(MANUAL, ["예측 불가 안내 확인: 안내 문구를 자동으로 찾지 못함(가격 언급 없음)"])
    notes = [f"안내 문구: '{disclaim.group(0)}'"] if disclaim else []
    return Verdict(FAIL if reasons else PASS, reasons, notes)


def _amount_forms(amount: int) -> list[str]:
    return [str(amount), f"{amount:,}"]


def _contains_number(text: str, form: str) -> bool:
    return re.search(r"(?<![\d,])" + re.escape(form) + r"(?![\d,]?\d)", text) is not None


def judge_balance_masking(record: dict) -> Verdict:
    """S11: 외부 LLM 입력에 계좌번호·금액 원문이 없고 자리표시자만 있으며, 최종 답에는 실제 금액이 복원된다."""
    answer = record.get("answer") or ""
    truth = record.get("truth") or {}
    llm = record.get("llm") or {}
    llm_text = llm.get("text") or ""
    amounts = [a for a in truth.get("amounts") or [] if isinstance(a, int) and abs(a) >= 10000]
    accounts = [a for a in truth.get("account_variants") or [] if a]
    reasons: list[str] = []
    notes: list[str] = []
    manual: list[str] = []
    if not llm.get("requests"):
        return Verdict(MANUAL, ["LLM 입력 확인: 외부 LLM 요청을 캡처하지 못함"])
    if any(a in llm_text for a in accounts):
        reasons.append("계좌번호 유출: 외부 LLM 입력에 계좌번호 원문")
    leaked = [a for a in amounts if any(_contains_number(llm_text, f) for f in _amount_forms(a))]
    if leaked:
        reasons.append(f"금액 유출: 외부 LLM 입력에 잔고 금액 원문 {len(leaked)}건")
    placeholders = set(m.group(0) for m in PLACEHOLDER_RE.finditer(llm_text))
    if not placeholders:
        reasons.append("자리표시자 없음: 외부 LLM 입력에 <AMOUNT_…>/<ACCOUNT_…> 자리표시자가 없음(잔고 미조회?)")
    else:
        notes.append(f"LLM 입력 자리표시자 {len(placeholders)}종")
    if PLACEHOLDER_RE.search(answer):
        reasons.append("복원 실패: 사용자 답에 자리표시자가 그대로 보임")
    if FALLBACK_PHRASE_RE.search(answer):
        reasons.append("복원 실패: 사용자 답에 '(이전 금액 n)' 중립 문구")
    if (marker := gate_rejected(answer)):
        reasons.append(f"게이트 거부: {marker}")
    if not amounts:
        manual.append("복원 확인: 잔고 금액이 모두 1만원 미만이라 복원 여부를 자동으로 볼 수 없음")
    else:
        shown = [a for a in amounts if any(_contains_number(answer, f) for f in _amount_forms(a))
                 or a in answer_prices(answer)]
        if shown:
            notes.append(f"답에 실제 금액 {len(shown)}건 복원")
        elif answer_prices(answer):
            reasons.append("복원 금액 불일치: 답의 금액이 실제 잔고 금액과 다름")
        else:
            reasons.append("금액 없음: 답에 잔고 금액이 없음")
    if reasons:
        return Verdict(FAIL, reasons, notes)
    if manual:
        return Verdict(MANUAL, manual, notes)
    return Verdict(PASS, [], notes)


def judge_diary_draft(record: dict) -> Verdict:
    """S12: 매매일지 초안이 나오고 게이트 거부가 없다(주문이 없으면 잔고 요약 초안이 정상)."""
    answer = record.get("answer") or ""
    reasons: list[str] = []
    notes: list[str] = []
    if (marker := gate_rejected(answer)):
        reasons.append(f"게이트 거부: {marker}")
    routed = record.get("routed_agent")
    if routed and routed != "diary_agent":
        reasons.append(f"라우팅: diary_agent가 아니라 {routed}")
    body = answer.split(FOOTNOTE_MARK)[0].strip()
    if len(body) < 80:
        reasons.append(f"초안 짧음: 본문 {len(body)}자")
    saves = int(record.get("diary_save_attempts") or 0)
    if saves:
        notes.append(f"일지 저장 시도 {saves}회(리허설에서는 저장하지 않음)")
    if reasons:
        return Verdict(FAIL, reasons, notes)
    if "일지" not in body:
        return Verdict(MANUAL, ["초안 확인: 본문에 '일지'라는 말이 없음 — 초안인지 눈으로 확인"], notes)
    return Verdict(PASS, [], notes)


JUDGES: dict[str, Callable[[dict], Verdict]] = {
    "news_and_price": judge_news_and_price,
    "compare_followup": judge_compare_followup,
    "price_cap_recommend": judge_price_cap_recommend,
    "earnings": judge_earnings,
    "holdings_news": judge_holdings_news,
    "no_price_forecast": judge_no_price_forecast,
    "balance_masking": judge_balance_masking,
    "diary_draft": judge_diary_draft,
}


def grade(record: dict) -> Verdict:
    """기록 하나를 판정한다. 인프라 → 예외·빈 답 → 장면별 순서."""
    infra = infra_reasons(record)
    if infra:
        return Verdict(INFRA, infra)
    common = common_failures(record)
    if common:
        return Verdict(FAIL, common)
    judge = JUDGES.get(record.get("judge") or "")
    if judge is None:
        return Verdict(MANUAL, [f"판정 없음: 알 수 없는 판정 '{record.get('judge')}'"])
    return judge(record)


# ---------------------------------------------------------------------------
# 요약
# ---------------------------------------------------------------------------


def reason_category(reason: str) -> str:
    return reason.split(":", 1)[0].strip()


def summarize(records: list[dict], *, top: int = 3) -> str:
    """jsonl 기록 목록 → 마크다운 요약(장면별 표 + 실패 회차 목록)."""
    scenes: dict[str, dict[str, Any]] = {}
    for rec in records:
        if rec.get("kind") == "setup":
            continue
        sid = rec.get("scene", "?")
        row = scenes.setdefault(sid, {"title": rec.get("title", ""), "counts": Counter(), "reasons": Counter(),
                                      "failed": []})
        verdict = rec.get("verdict") or {}
        status = verdict.get("status", MANUAL)
        row["counts"][status] += 1
        if status in (FAIL, INFRA, MANUAL):
            for r in verdict.get("reasons") or []:
                row["reasons"][reason_category(r)] += 1
            row["failed"].append(rec)
    lines = [
        "| 장면 | 내용 | 실행 | 통과 | 실패 | 인프라 실패 | 수동 확인 | 통과율(인프라 제외) | 실패 사유 상위 |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for sid in sorted(scenes, key=_scene_order):
        row = scenes[sid]
        c = row["counts"]
        total = sum(c.values())
        judged = total - c[INFRA]
        rate = f"{c[PASS]}/{judged} ({c[PASS] / judged:.0%})" if judged else "-"
        top_reasons = ", ".join(f"{k} {v}" for k, v in row["reasons"].most_common(top)) or "-"
        lines.append(f"| {sid} | {row['title']} | {total} | {c[PASS]} | {c[FAIL]} | {c[INFRA]} | {c[MANUAL]} "
                     f"| {rate} | {top_reasons} |")
    detail: list[str] = []
    for sid in sorted(scenes, key=_scene_order):
        for rec in scenes[sid]["failed"]:
            v = rec.get("verdict") or {}
            label = STATUS_LABELS.get(v.get("status"), v.get("status"))
            detail.append(f"- **{sid} {rec.get('rep', 0) + 1}회차 · {label}** — "
                          + "; ".join(v.get("reasons") or ["(사유 없음)"]))
    if detail:
        lines += ["", "### 실패·인프라 실패·수동 확인 회차", "",
                  "답 원문과 도구 호출 목록은 `runs.jsonl`의 같은 회차 줄(`answer`, `calls`)에 있다.", "", *detail]
    return "\n".join(lines) + "\n"


def _scene_order(sid: str) -> tuple[int, str]:
    m = re.match(r"S(\d+)", sid)
    return (int(m.group(1)) if m else 999, sid)
