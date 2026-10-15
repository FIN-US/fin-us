"""뉴스·공시 신호의 유의성 채점."""

import logging
from typing import Any, get_args, Literal, NamedTuple, Optional, Sequence

from statistics import pstdev

from backend.ai import llm
from backend.ai.llm import _json_objects_from_text
from backend.core.config import SIGNAL_SCORE_MAX, SIGNAL_SCORE_MIN, SIGNAL_SCORE_THRESHOLD
from backend.core.schemas import AnalysisReport, UrgencyLevel
from backend.pii.egress import personal, Segment

logger = logging.getLogger(__name__)


# schemas.AnalysisReport.urgency의 Literal 값에서 파생 — 스키마와 두 곳이 갈라지는 것을 막는다.
# AnalysisReport.urgency: UrgencyLevel = Literal["low", "normal", "high", "critical"]
# 원소 타입을 str이 아니라 UrgencyLevel로 선언해야 `raw in _URGENCY_LEVELS`가
# 타입 체커에서도 좁히기로 작동한다 — get_args의 반환은 tuple[Any, ...]이라
# 선언을 붙이지 않으면 frozenset[str]로 추론돼 아래 urgency가 Literal로 좁혀지지 않는다.
_URGENCY_LEVELS: frozenset[UrgencyLevel] = frozenset(
    get_args(AnalysisReport.model_fields["urgency"].annotation)
)

# score_signal이 쓸 수 있는 provider — llm_chat의 str 반환 오버로드와 같은 집합이다.
# "nat"은 여기 없다. NAT는 멀티에이전트 왕복이라 1차 필터의 전제(경량·저비용)를
# 깨고, 반환값 NatAnswer가 str 서브클래스여서 파싱은 그대로 통과해 버린다 —
# 즉 잘못 라우팅돼도 증상이 남지 않는다.
_ScoringProvider = Literal["openai", "anthropic", "ollama"]
_SCORING_PROVIDERS: frozenset[_ScoringProvider] = frozenset(get_args(_ScoringProvider))
_DEFAULT_SCORING_PROVIDER: _ScoringProvider = "ollama"


class SignalScore(NamedTuple):
    """2차 필터가 signal 하나에 대해 내린 채점 결과 (#298).

    ``score``/``reason``/``uncertainty``가 None인 것과 0/""인 것은 다른 상태다.
    None은 "채점하지 못했다", 0은 "무관/중립이라고 채점했다"이다 (#122·#162).
    """

    score: Optional[int]
    reason: Optional[str]
    uncertainty: Optional[float]
    headline_scores: tuple[int, ...]
    is_significant: bool


# 채점에 실패했을 때의 결과. is_significant=True가 핵심이다 (REQ-04 fail-open):
# 경량 LLM이 죽었다고 상세 분석까지 건너뛰면 진짜 대형 악재를 통째로 놓친다.
# 점수는 null로 남겨 "통과시켰지만 몇 점인지는 모른다"를 사후에 구분할 수 있게 한다.
_FAIL_OPEN_SIGNAL_SCORE = SignalScore(None, None, None, (), True)
# 채점 이전에 걸러진 signal(빈 내용, 직전과 동일 내용). LLM을 부르지도 않았으므로 점수가 없다.
_SKIPPED_SIGNAL_SCORE = SignalScore(None, None, None, (), False)

# reason은 알림 한 줄에 들어간다. 모델이 장문을 뱉으면 잘라 쓴다.
_SIGNAL_REASON_MAX_CHARS = 120
# 채점 프롬프트에 넣는 signal 본문 상한 (기존 YES/NO 필터와 동일하게 유지).
_SIGNAL_SNIPPET_CHARS = 1000

def _coerce_signal_score(value: Any) -> Optional[int]:
    """임의의 JSON 값을 -3~+3 정수로 좁힌다. 숫자로 볼 수 없으면 None.

    소수(2.5)는 반올림한다 — 프롬프트는 정수를 요구하지만 경량 모델은 자주 어긴다.
    레인지를 벗어난 값(7, -9)은 버리지 않고 끝값으로 자른다: "아주 큰 호재"라는
    방향 정보 자체는 유효하고, 이걸 파싱 실패로 처리하면 하필 대형 신호에서만
    fail-open이 잦아진다. bool은 int의 하위형이라 True가 1점으로 새는 것을 막는다.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        try:
            value = float(value.strip())
        except ValueError:
            return None
    if not isinstance(value, (int, float)):
        return None
    if value != value or value in (float("inf"), float("-inf")):  # NaN/±inf
        return None
    return max(SIGNAL_SCORE_MIN, min(SIGNAL_SCORE_MAX, int(round(value))))


def signal_score_uncertainty(headline_scores: Sequence[int]) -> Optional[float]:
    """기사별 점수의 표준편차. 기사가 2건 미만이면 None.

    LLM에게 확신도를 직접 묻지 않고 코드로 계산한다. headline_scores도 결국 같은
    모델의 자기보고이므로 이것이 "객관적 사실"이 되는 것은 아니다 — 다만 항목별로
    따로 받은 점수의 흩어짐은 한 번에 물어본 확신도보다 다루기 쉽다. 계산 규칙이
    코드에 고정돼 있어 프롬프트가 바뀌어도 일관되고, 사람이 같은 기사에 매긴 점수로
    똑같이 계산해 대조할 수 있다.

    모집단 표준편차(pstdev)를 쓴다: 이 기사 묶음은 더 큰 모집단에서 뽑은 표본이
    아니라 판단에 실제로 쓴 전부다. 1건이면 0.0이 아니라 None이다 — 0.0은
    "기사들이 완전히 일치했다"는 뜻이고, 1건은 흩어짐을 정의할 수 없는 상태다.
    """
    if len(headline_scores) < 2:
        return None
    return pstdev(headline_scores)


def parse_signal_score(raw: str) -> Optional[SignalScore]:
    """채점 응답 텍스트에서 SignalScore를 뽑는다. 실패하면 None.

    None은 호출부에서 fail-open(유의미 취급)으로 이어진다. 그래서 "적당히 넘겨
    짚기"보다 명확히 실패로 떨어뜨리는 편이 안전하다 — 점수를 지어내면 그 값이
    DB와 평가셋에 그대로 남지만, 실패는 null로 남아 구분된다.
    """
    for data in _json_objects_from_text(raw or ""):
        score = _coerce_signal_score(data.get("score"))
        if score is None:
            continue

        raw_reason = data.get("reason")
        reason: Optional[str] = None
        if isinstance(raw_reason, str):
            # 알림 한 줄에 들어가므로 줄바꿈을 접는다.
            collapsed = " ".join(raw_reason.split())
            reason = collapsed[:_SIGNAL_REASON_MAX_CHARS] or None

        raw_headlines = data.get("headline_scores")
        headline_scores: tuple[int, ...] = ()
        if isinstance(raw_headlines, list):
            # 숫자로 볼 수 없는 원소는 버린다. 개수가 기사 수와 어긋날 수 있지만,
            # 표준편차는 "실제로 채점된 기사들"의 흩어짐이면 충분하다.
            headline_scores = tuple(
                coerced
                for coerced in (_coerce_signal_score(item) for item in raw_headlines)
                if coerced is not None
            )

        return SignalScore(
            score=score,
            reason=reason,
            uncertainty=signal_score_uncertainty(headline_scores),
            headline_scores=headline_scores,
            is_significant=abs(score) >= SIGNAL_SCORE_THRESHOLD,
        )
    return None


def _signal_snippet(signal_content: str) -> str:
    """채점에 넣을 본문을 상한까지 자르되, 잘려 나간 마지막 줄은 버린다.

    signal은 기사 한 건이 한 줄이다. 상한에서 반토막 난 줄을 남기면 모델은 그것을
    온전한 기사 1건으로 세어 점수를 매기고, 그 값이 headline_scores를 거쳐
    signal_uncertainty(표준편차)까지 오염시킨다.

    상한 안에 줄바꿈이 하나도 없으면 통째로 한 기사이므로 자른 그대로 쓴다 —
    이때는 버릴 온전한 줄이 없어서, 버리면 채점할 내용 자체가 사라진다.
    """
    if len(signal_content) <= _SIGNAL_SNIPPET_CHARS:
        return signal_content

    truncated = signal_content[:_SIGNAL_SNIPPET_CHARS]
    head, separator, _partial = truncated.rpartition("\n")
    return head if separator else truncated


def _build_signal_score_prompt(stock: str, source: str, snippet: Segment) -> list[str | Segment]:
    """채점 프롬프트. 단계별 기준을 모두 적어 모델이 축을 스스로 상상하지 않게 한다.

    레인지를 -3~+3으로 좁게 잡은 것은 의도다. 0~100 같은 넓은 축에서 경량 모델은
    같은 기사에 매번 다른 점수를 준다 — 좁은 축은 재현성을 사고, 잃는 해상도는
    이 필터가 애초에 필요로 하지 않는다.
    """
    headline_count = len([line for line in snippet.text.splitlines() if line.strip()])
    return [
        f"당신은 전문 주식 분석가입니다. 다음은 '{stock}' 종목에 대한 최신 외부 signal입니다.\n"
        f"signal 출처: {source}\n\n"
        "--- signal 내용 ---\n",
        snippet,
        "\n------------------\n\n"
        f"이 signal이 '{stock}'의 투자 판단에 미치는 영향을 아래 기준으로 채점하십시오.\n"
        "+3: 실적 서프라이즈, 대형 수주·계약, M&A 등 명확한 대형 호재\n"
        "+2: 방향이 분명한 호재 (신규 대형 고객사, 의미 있는 가이던스 상향 등)\n"
        "+1: 약한 호재 또는 우호적인 분위기\n"
        " 0: 투자 판단과 무관하거나 중립 (홍보성 기사, 단순 시황 나열, 반복되는 내용)\n"
        "-1: 약한 악재\n"
        "-2: 방향이 분명한 악재 (가이던스 하향, 주요 고객 이탈 등)\n"
        "-3: 실적 쇼크, 대형 소송·제재, 회계 이슈 등 명확한 대형 악재\n\n"
        f"signal 내용은 기사 {headline_count}건이 줄 단위로 이어져 있습니다. "
        "headline_scores에는 각 기사를 같은 기준으로 따로 채점한 정수를 원문 순서대로 담으십시오.\n\n"
        "반드시 아래 JSON 객체 하나만 출력하고 다른 설명은 붙이지 마십시오.\n"
        '{"score": <-3~3 정수>, "reason": "<점수 근거 한 줄, 40자 이내>", '
        '"headline_scores": [<-3~3 정수>, ...]}',
    ]


async def score_signal(
    stock: str,
    signal_content: str | Segment,
    last_signal_content: Optional[str] = None,
    *,
    source: str = "signal",
    provider: str = "ollama",
) -> SignalScore:
    """외부 signal을 -3~+3으로 채점한다 (#298).

    로컬 초경량 모델(예: gemma4) 또는 경량 API 모델(예: gpt-5.4-mini)을 1차
    필터로 사용해 비용과 정확도의 균형을 맞춘다.

    ``provider``는 ``_SCORING_PROVIDERS``의 값이어야 한다. 그 밖의 값은 경고를
    남기고 기본값으로 되돌린다 — llm_chat은 매칭되지 않는 provider_key를 전부
    NAT로 보내므로, 그대로 넘기면 오타 하나가 채점을 멀티에이전트 왕복으로
    바꿔 놓는다.

    호출·파싱에 실패하면 예외를 올리지 않고 fail-open한다 — 유의미로 통과시키되
    점수는 null로 남긴다 (REQ-04 놓침 방지). 비식별화 실패로 전송이 차단된 경우
    (``pii_egress.EgressBlocked``)도 같은 갈래다 — 채점은 fail-open이어도 원문은 나가지 않는다.

    ``signal_content``: 뉴스·공시 원문이면 호출부가 ``pii_egress.public()``으로 감싼다.
    맨 문자열은 출처를 모르는 값으로 보고 마스킹한다(#395).
    """
    signal = signal_content if isinstance(signal_content, Segment) else personal(signal_content)
    if not signal.text:
        return _SKIPPED_SIGNAL_SCORE

    if signal.text == last_signal_content:
        return _SKIPPED_SIGNAL_SCORE

    prompt = _build_signal_score_prompt(
        stock, source, Segment(_signal_snippet(signal.text), public=signal.public)
    )

    # provider의 출처는 환경변수(scheduler.monitor.FILTER_PROVIDER)라 검증된 적이 없다.
    # llm_chat의 마지막 줄은 catch-all이므로 매칭되지 않는 값은 전부 NAT로 떨어진다
    # — 아래 fail-open은 호출이 실패했을 때의 장치이지 오라우팅을 막지 못한다.
    # 계약 밖의 값은 여기서 기본값으로 되돌리고 로그를 남긴다.
    if provider in _SCORING_PROVIDERS:
        scoring_provider = provider
    else:
        logger.warning(
            "signal 채점 provider=%r는 지원 목록 %s 밖입니다 — %r로 되돌립니다. "
            "NEWS_FILTER_PROVIDER 설정을 확인하세요.",
            provider,
            sorted(_SCORING_PROVIDERS),
            _DEFAULT_SCORING_PROVIDER,
        )
        scoring_provider = _DEFAULT_SCORING_PROVIDER

    try:
        # 설정된 provider(ollama, openai 등)에 따라 경량 모델 호출
        raw = await llm.llm_chat(scoring_provider, prompt)
    except Exception as e:
        logger.error(
            "signal 채점 호출 실패 (%s, %s, %s): %s — 유의미로 통과시킵니다(fail-open)",
            source,
            stock,
            provider,
            e,
        )
        return _FAIL_OPEN_SIGNAL_SCORE

    parsed = parse_signal_score(str(raw))
    if parsed is None:
        logger.warning(
            "signal 채점 응답을 파싱하지 못했습니다 [%s:%s] (Provider: %s) — "
            "유의미로 통과시킵니다(fail-open)",
            source,
            stock,
            provider,
        )
        return _FAIL_OPEN_SIGNAL_SCORE

    logger.info(
        "signal 채점 [%s:%s] (Provider: %s): score=%s, 유의미=%s, 기사별=%s, 흩어짐=%s, 근거=%s",
        source,
        stock,
        provider,
        parsed.score,
        parsed.is_significant,
        list(parsed.headline_scores),
        parsed.uncertainty,
        parsed.reason,
    )
    return parsed


async def check_signal_significance(
    stock: str,
    signal_content: str,
    last_signal_content: Optional[str] = None,
    *,
    source: str = "signal",
    provider: str = "ollama"
) -> bool:
    """외부 signal이 투자 관점에서 유의미한 변화인지 판단합니다.

    판정은 :func:`score_signal`의 점수를 ``|score| >= SIGNAL_SCORE_THRESHOLD``로
    환산한 것이다 (#298 이전에는 경량 LLM에 YES/NO를 직접 물었다).

    점수·근거·불확실성까지 필요한 호출부는 :func:`score_signal`을 직접 쓴다 —
    감시 파이프라인(scheduler)이 그렇게 한다. 이 함수는 "유의미한가"만 알면 되는
    호출부를 위한 얇은 래퍼다.
    """
    scored = await score_signal(
        stock,
        signal_content,
        last_signal_content,
        source=source,
        provider=provider,
    )
    return scored.is_significant


async def check_news_significance(
    stock: str,
    news_content: str,
    last_news_content: Optional[str] = None,
    provider: str = "ollama",
) -> bool:
    return await check_signal_significance(
        stock,
        news_content,
        last_news_content,
        source="news",
        provider=provider,
    )
