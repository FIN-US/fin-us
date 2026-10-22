"""순위 TR 가격 범위 인자의 방향을 KIS 도구 안내가 바르게 알려 주는지 (#441).

upstream ``find_api_detail`` 설명은 ``(가격 ~)``·``(~ 가격)``뿐이라 모델이 "30만원 이하"를
``fid_input_price_1=300000``(하한)으로 넣어 30만원 이상 종목만 받고 "조건에 맞는 종목이 없다"고
단정했다. 방향의 근거는 upstream 예제 호출값이다(``finus_api._KIS_TRADING_TOOL_GUIDE`` 위 주석):
``fid_input_price_1``이 하한, ``fid_input_price_2``가 상한이다.

문구를 그대로 재진술하지 않고, 에이전트가 실제로 받는 도구 설명에서 두 인자가 각각 어느 방향으로
설명되는지만 본다 — 안내를 지우거나 두 인자의 뜻을 뒤바꾸면 빨개진다.
"""

import asyncio
import re

import pytest

from finus_agents import finus_api

# upstream configs/domestic_stock.json의 market_cap 설명 그대로 — 원격 문서가 이 모호한 표기만
# 줄 때도 안내가 방향을 정해 주는지 본다.
_UPSTREAM_DOC = (
    "fid_input_price_1: 입력값 없을때 전체 (가격 ~)\n"
    "fid_input_price_2: 입력값 없을때 전체 (~ 가격)"
)


@pytest.fixture
def description(monkeypatch) -> str:
    async def _fake_doc(_config):
        return _UPSTREAM_DOC

    monkeypatch.setattr(finus_api, "_fetch_mcp_tool_documentation", _fake_doc)
    config = finus_api.FinusAccountBalanceReadonlyConfig()
    return asyncio.run(finus_api._build_kis_mcp_description("KIS 조회 전용", config))


def _guide_lines(text: str, field: str) -> list[str]:
    """원격 문서 줄을 빼고, 안내에서 *field*를 다루는 줄만 고른다."""
    upstream = set(_UPSTREAM_DOC.splitlines())
    return [ln for ln in text.splitlines() if field in ln and ln not in upstream]


@pytest.mark.parametrize(
    "field, bound, direction, wrong",
    [
        ("fid_input_price_1", "하한", "이상", "상한"),
        ("fid_input_price_2", "상한", "이하", "하한"),
    ],
)
def test_ranking_price_field_direction(description, field, bound, direction, wrong):
    lines = _guide_lines(description, field)
    assert lines, f"도구 안내가 {field}의 뜻을 알려 주지 않는다"
    # 인자 이름 바로 뒤의 설명(다음 백틱 전까지)에서 방향을 읽는다.
    pattern = re.compile(rf"`{field}`([^`]*)")
    meanings = [m.group(1) for ln in lines for m in pattern.finditer(ln)]
    assert any(bound in m and direction in m for m in meanings), meanings
    assert not any(wrong in m for m in meanings), meanings


def test_ranking_price_example_is_lower_then_upper(description):
    """예시 값도 하한 < 상한이어야 한다 — 예시만 뒤집혀도 모델은 예시를 따른다."""
    found = re.findall(
        r'"fid_input_price_1":"(\d+)","fid_input_price_2":"(\d+)"', description
    )
    assert found, "도구 안내에 가격 범위 예시가 없다"
    for low, high in found:
        assert int(low) < int(high)
