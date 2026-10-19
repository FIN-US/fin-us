"""시세 TR을 종목명으로 부르면 래퍼가 종목코드를 찾아 채운다.

Kis Trading MCP의 종목명 자동 변환은 pdno만 채워 inquire_price 등은 종목명으로 부를 수
없었고, 에이전트는 find_stock_code 결과("종목코드는 005930입니다")만으로 답을 끝냈다.
"""

import json

import pytest

from finus_agents import finus_api

_FOUND = json.dumps({"ok": True, "data": {"found": True, "stock_code": "035720"}})
_NOT_FOUND = json.dumps({"ok": True, "data": {"found": False, "message": "종목을 찾을 수 없습니다"}})


@pytest.fixture
def remote(monkeypatch):
    calls: list[dict] = []
    replies = {"find_stock_code": _FOUND}

    async def fake_remote(**kwargs):
        calls.append(kwargs["arguments"])
        return replies.get(kwargs["arguments"]["api_type"], '{"ok":true,"data":{"price":"31950"}}')

    monkeypatch.setattr(finus_api, "_mcp_call_tool_remote", fake_remote)
    calls_replies = (calls, replies)
    return calls_replies


@pytest.fixture
def config():
    return finus_api.FinusAccountBalanceReadonlyConfig()


async def test_stock_name_is_resolved_to_code(remote, config):
    calls, _ = remote

    resolved = await finus_api._resolve_stock_name_for_quote(
        "domestic_stock",
        {"api_type": "inquire_price", "params": {"stock_name": "카카오", "env_dv": "demo"}},
        config,
    )

    assert calls == [{"api_type": "find_stock_code", "params": {"stock_name": "카카오"}}]
    assert resolved == {
        "api_type": "inquire_price",
        "params": {"fid_cond_mrkt_div_code": "J", "env_dv": "demo", "fid_input_iscd": "035720"},
    }


async def test_not_found_returns_lookup_result(remote, config):
    calls, replies = remote
    replies["find_stock_code"] = _NOT_FOUND

    resolved = await finus_api._resolve_stock_name_for_quote(
        "domestic_stock", {"api_type": "inquire_price", "params": {"stock_name": "없는회사"}}, config,
    )

    assert resolved == _NOT_FOUND


@pytest.mark.parametrize(
    "tool_name, arguments",
    [
        # 종목코드가 이미 있으면 그대로 둔다.
        ("domestic_stock", {"api_type": "inquire_price", "params": {"fid_input_iscd": "005930", "stock_name": "삼성전자"}}),
        # 순위 TR의 fid_input_iscd는 시장 코드라 대상이 아니다.
        ("domestic_stock", {"api_type": "volume_rank", "params": {"stock_name": "삼성전자"}}),
        # 국내주식이 아닌 도구는 대상이 아니다.
        ("overseas_stock", {"api_type": "inquire_price", "params": {"stock_name": "AAPL"}}),
        # 종목명이 없으면 대상이 아니다.
        ("domestic_stock", {"api_type": "inquire_price", "params": {}}),
    ],
)
async def test_untouched_when_not_applicable(remote, config, tool_name, arguments):
    calls, _ = remote

    assert await finus_api._resolve_stock_name_for_quote(tool_name, arguments, config) is arguments
    assert calls == []


async def test_call_path_queries_with_resolved_code(remote, config):
    """KIS 호출이 모두 지나는 _call_kis_mcp_and_record가 변환을 거친다."""
    calls, _ = remote

    await finus_api._call_kis_mcp_and_record(
        "domestic_stock", {"api_type": "inquire_price", "params": {"stock_name": "카카오"}}, config,
    )

    assert [c["api_type"] for c in calls] == ["find_stock_code", "inquire_price"]
    assert calls[1]["params"]["fid_input_iscd"] == "035720"
