"""#468: 사용자 발화의 금액 조건을 NAT 조회 인자로 쓸 수 있게 같은 요청에 원값을 함께 싣는다.

"30만원 이하 반도체 대형주 추천해줘"는 `llm_chat`이 마스킹해 NAT(그리고 외부 LLM)에는
``<AMOUNT_…> 이하 …``로 간다. 원값은 backend에만 있으므로, NAT이 순위 조회 가격 인자를 되돌릴 수
있게 `_llm_nat_chat`이 **금액 종류만** `x-finus-input-amounts` 헤더로 싣는다. 본문(외부 LLM으로
가는 것)은 여전히 마스킹된 발화뿐이어야 한다.
"""

import json
from pathlib import Path

import pytest

from backend.ai import llm as ai_llm
from backend.pii.registry import _REGISTRY, active_mapping, live_values_in
from backend.pii.mask import mask_pii

_HEADER = ai_llm.NAT_INPUT_AMOUNTS_HEADER


class _FakeResponse:
    status_code = 200
    text = "{}"

    def json(self):
        return {"choices": [{"message": {"content": "답변"}}]}


@pytest.fixture
def nat_requests(monkeypatch):
    requests: list[dict] = []

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, *, headers=None, json=None):
            requests.append({"url": url, "headers": dict(headers or {}), "json": json})
            return _FakeResponse()

    monkeypatch.setattr(ai_llm.httpx, "AsyncClient", FakeAsyncClient)
    return requests


@pytest.mark.asyncio
async def test_amount_travels_in_header_and_body_stays_masked(nat_requests):
    """뮤테이션: `_llm_nat_chat`에서 `headers.update(nat_input_amount_headers(...))`를 빼면 red."""
    await ai_llm.llm_chat("nat", "30만원 이하 반도체 대형주 추천해줘")

    sent = nat_requests[0]
    body = json.dumps(sent["json"], ensure_ascii=False)
    # 외부 LLM으로 가는 본문에는 금액 원문이 없다.
    assert "30만" not in body
    content = sent["json"]["messages"][0]["content"]
    placeholder = content.split(" ", 1)[0]
    assert placeholder.startswith("<AMOUNT_")
    # 원값은 같은 요청의 헤더로만 간다. HTTP 헤더라 ASCII여야 한다.
    raw = sent["headers"][_HEADER]
    assert raw.isascii()
    assert json.loads(raw) == {placeholder: "30만원"}
    assert not _REGISTRY  # 블록을 벗어나면 등록이 지워진다(#354 계약 그대로)


@pytest.mark.asyncio
async def test_only_amounts_are_sent(nat_requests):
    """계좌번호·수량은 조회 조건으로 되돌릴 자리가 없으니 NAT에 넘기지 않는다."""
    await ai_llm.llm_chat("nat", "계좌 12345678-01, 삼성전자 3주, 5만원 이상 30만원 이하 종목")

    amounts = json.loads(nat_requests[0]["headers"][_HEADER])
    assert sorted(amounts.values()) == ["30만원", "5만원"]
    assert all(ph.startswith("<AMOUNT_") for ph in amounts)
    assert "12345678" not in nat_requests[0]["headers"][_HEADER]


@pytest.mark.asyncio
async def test_no_header_without_amounts(nat_requests):
    await ai_llm.llm_chat("nat", "반도체 대형주 추천해줘")

    assert _HEADER not in nat_requests[0]["headers"]


@pytest.mark.asyncio
async def test_no_header_outside_a_live_request(nat_requests):
    """등록소에 살아 있는 매핑이 없으면(직접 호출·이전 턴 토큰) 아무것도 싣지 않는다."""
    _, mapping = mask_pii("30만원 이하")
    stale = next(iter(mapping))

    await ai_llm._llm_nat_chat(f"{stale} 이하 종목")

    assert _HEADER not in nat_requests[0]["headers"]


def test_live_values_are_scoped_to_the_active_block():
    masked, mapping = mask_pii("30만원 이하, 삼성전자 3주")
    with active_mapping(mapping):
        inside = live_values_in(masked, kind="AMOUNT")
    assert list(inside.values()) == ["30만원"]
    assert live_values_in(masked, kind="AMOUNT") == {}


def test_header_name_matches_the_nat_side():
    """NAT은 별도 프로세스라 상수를 import로 공유하지 못한다 — 문자열이 같은지 고정한다."""
    guard = Path(__file__).resolve().parents[2] / "agents" / "src" / "finus_agents" / "pii_guard.py"
    assert f'INPUT_AMOUNTS_HEADER = "{_HEADER}"' in guard.read_text(encoding="utf-8")
