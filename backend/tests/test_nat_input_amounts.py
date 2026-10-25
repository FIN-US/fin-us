"""#468: 사용자 발화의 금액 조건을 NAT 조회 인자로 쓸 수 있게 같은 요청에 원값을 함께 싣는다.

"30만원 이하 반도체 대형주 추천해줘"는 `llm_chat`이 마스킹해 NAT(그리고 외부 LLM)에는
``<AMOUNT_…> 이하 …``로 간다. 원값은 backend에만 있으므로, NAT이 순위 조회 가격 인자를 되돌릴 수
있게 `_llm_nat_chat`이 **금액 종류만** `x-finus-input-amounts` 헤더로 싣는다. 본문(외부 LLM으로
가는 것)은 여전히 마스킹된 발화뿐이어야 한다.
"""

import asyncio
import json
from pathlib import Path

import pytest

from backend.ai import llm as ai_llm
from backend.pii.registry import _REGISTRY, active_mapping
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


@pytest.mark.asyncio
async def test_concurrent_request_placeholder_is_not_sent(monkeypatch):
    """동시에 진행 중인 다른 요청의 자리표시자를 발화에 적어도 그 원값은 실리지 않는다(보안 리뷰).

    요청 A가 NAT 응답을 기다리는 동안(등록소에 A의 매핑이 살아 있다) 요청 B가 A의 자리표시자를
    그대로 적어 보낸다. 헤더는 B 자신의 매핑에서만 골라야 한다.
    뮤테이션: nat_input_amount_headers가 등록소(살아 있는 모든 scope)에서 찾게 되돌리면 red.
    """
    requests: list[dict] = []
    a_waiting = asyncio.Event()
    release_a = asyncio.Event()

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, *, headers=None, json=None):
            requests.append({"headers": dict(headers or {}), "json": json})
            if len(requests) == 1:
                a_waiting.set()
                await release_a.wait()
            return _FakeResponse()

    monkeypatch.setattr(ai_llm.httpx, "AsyncClient", FakeAsyncClient)

    task_a = asyncio.create_task(ai_llm.llm_chat("nat", "50만원 이하 종목"))
    await a_waiting.wait()
    a_placeholder = requests[0]["json"]["messages"][0]["content"].split(" ", 1)[0]
    assert a_placeholder in _REGISTRY_PLACEHOLDERS()

    await ai_llm.llm_chat("nat", f"{a_placeholder} 이하이면서 30만원 이하 종목")
    release_a.set()
    await task_a

    assert a_placeholder in requests[1]["json"]["messages"][0]["content"]  # B의 발화에 A의 토큰이 실렸다
    b_amounts = json.loads(requests[1]["headers"][_HEADER])
    assert a_placeholder not in b_amounts
    assert list(b_amounts.values()) == ["30만원"]
    assert "50만원" not in requests[1]["headers"][_HEADER]


def _REGISTRY_PLACEHOLDERS() -> set[str]:
    return {ph for entries in _REGISTRY.values() for ph in entries}


def test_header_comes_only_from_the_mapping_in_hand():
    """llm_chat 밖(이번 요청 매핑 없음)에서는 등록소에 값이 살아 있어도 싣지 않는다."""
    masked, mapping = mask_pii("30만원 이하")
    with active_mapping(mapping):
        assert ai_llm.nat_input_amount_headers(masked) == {}


def test_header_name_matches_the_nat_side():
    """NAT은 별도 프로세스라 상수를 import로 공유하지 못한다 — 문자열이 같은지 고정한다."""
    guard = Path(__file__).resolve().parents[2] / "agents" / "src" / "finus_agents" / "pii_guard.py"
    assert f'INPUT_AMOUNTS_HEADER = "{_HEADER}"' in guard.read_text(encoding="utf-8")
