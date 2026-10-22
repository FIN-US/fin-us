"""#442: upstream ``main``과 판정표가 어긋나면 잡는 drift 검사의 오프라인 테스트.

Kis Trading MCP 서버는 도구 호출마다 upstream ``main``의 예제를 받아 실행하고, 버전을 고정하는
설정이 없다. 그래서 주기 워크플로(``.github/workflows/kis-upstream-drift.yml``)가
``agents/scripts/kis_tr_verdicts.py --check main``으로 판정표를 다시 만들어 대조한다. 네트워크는
그 워크플로에만 있고, 여기서는 받아 온 뒤의 판정·대조 로직을 가짜 upstream 트리로 본다.
"""

import copy
import importlib.util
import json
from pathlib import Path

import pytest

from finus_agents.pii_guard import PUBLIC_KIS_QUOTE_APIS

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "kis_tr_verdicts.py"
_PINNED = json.loads(
    (Path(__file__).parent / "fixtures" / "kis_domestic_stock_tr_verdicts.json").read_text(encoding="utf-8")
)


@pytest.fixture(scope="module")
def gen():
    spec = importlib.util.spec_from_file_location("kis_tr_verdicts", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _row(table: dict, api_type: str) -> dict:
    return next(r for r in table["trs"] if r["api_type"] == api_type)


# ---------------------------------------------------------------------------
# 고정 판정표와 공개 시세 목록
# ---------------------------------------------------------------------------

def test_pinned_table_has_no_public_quote_violation(gen):
    """drift 검사와 같은 함수로, 지금의 판정표·공개 시세 목록이 깨끗하다는 것을 확인한다.
    (같은 성질을 ``test_kis_passthrough_masking``도 보지만, 워크플로가 쓰는 함수가 같은 판정을 내는지가 여기 몫이다.)"""
    assert gen.public_quote_violations(_PINNED, PUBLIC_KIS_QUOTE_APIS) == []


@pytest.mark.parametrize("api_type", sorted(
    api for tool, api in PUBLIC_KIS_QUOTE_APIS if api not in {"find_stock_code", "find_api_detail"}
))
def test_account_input_on_any_public_quote_is_flagged(gen, api_type):
    """판정표에서 공개 시세 TR 하나가 계좌 입력을 받게 되면 개인정보 경계 위반으로 잡는다.

    뮤테이션: ``public_quote_violations``의 ``account_input`` 검사를 지우면 전 행이 red.
    """
    table = copy.deepcopy(_PINNED)
    _row(table, api_type)["account_input"] = "CANO"

    problems = gen.public_quote_violations(table, PUBLIC_KIS_QUOTE_APIS)

    assert problems == [f"{api_type}: 계좌·사용자 입력을 받는다 (CANO)"]


@pytest.mark.parametrize(("field", "value", "expected"), [
    ("verdict", "write", "판정이 read가 아니다"),
    ("tr_ids", ["TTTC8434R"], "시세 계열(F·H)이 아닌 TR ID"),
])
def test_public_quote_that_stops_being_a_quote_is_flagged(gen, field, value, expected):
    table = copy.deepcopy(_PINNED)
    _row(table, "inquire_price")[field] = value

    problems = gen.public_quote_violations(table, PUBLIC_KIS_QUOTE_APIS)

    assert len(problems) == 1 and expected in problems[0] and problems[0].startswith("inquire_price:")


def test_public_quote_missing_from_upstream_is_flagged(gen):
    """upstream에서 예제가 사라지면 근거가 없어진 것이다 — 조용히 통과하지 않는다."""
    table = copy.deepcopy(_PINNED)
    table["trs"] = [r for r in table["trs"] if r["api_type"] != "market_cap"]

    assert gen.public_quote_violations(table, PUBLIC_KIS_QUOTE_APIS) == ["market_cap: 판정표(upstream 예제)에 없다"]


# ---------------------------------------------------------------------------
# 판정표 대조
# ---------------------------------------------------------------------------

def test_same_rows_with_a_new_commit_is_not_drift(gen):
    """upstream 커밋만 움직이고 판정이 같으면 실패하지 않는다 — 주기 검사가 늘 빨갛지 않게."""
    fresh = copy.deepcopy(_PINNED)
    fresh["_meta"]["upstream_commit"] = "0" * 40

    assert gen.diff_tables(_PINNED, fresh) == []


def test_any_row_change_is_drift(gen):
    """행이 추가·삭제·변경되면 전부 보고한다. 허용 목록(``read`` 행)도 판정표에 기대므로 공개 시세가
    아닌 TR의 변화도 drift다.

    뮤테이션: ``diff_tables``가 ``[]``를 돌려주게 하면 red.
    """
    fresh = copy.deepcopy(_PINNED)
    _row(fresh, "inquire_balance")["verdict"] = "write"
    fresh["trs"] = [r for r in fresh["trs"] if r["api_type"] != "disparity"]
    fresh["trs"].append({"api_type": "brand_new_tr", "tr_ids": ["FHX"], "verdict": "read"})

    lines = gen.diff_tables(_PINNED, fresh)

    assert "바뀐 TR: inquire_balance.verdict: 'read' -> 'write'" in lines
    assert "사라진 TR: disparity" in lines
    assert any(line.startswith("추가된 TR: brand_new_tr") for line in lines)
    assert len(lines) == 3


# ---------------------------------------------------------------------------
# 끝에서 끝까지 — 가짜 upstream 트리에서 예제 소스가 바뀌면 check가 실패한다
# ---------------------------------------------------------------------------

_QUOTE_SRC = '''# [국내주식] 기본시세 > 주식현재가 시세
API_URL = "/uapi/domestic-stock/v1/quotations/inquire-price"

def inquire_price(env_dv: str, fid_cond_mrkt_div_code: str, fid_input_iscd: str):
    tr_id = "FHKST01010100"
    params = {"FID_COND_MRKT_DIV_CODE": fid_cond_mrkt_div_code, "FID_INPUT_ISCD": fid_input_iscd}
    return ka._url_fetch(API_URL, tr_id, "", params)
'''

# upstream이 같은 예제에 계좌 입력을 붙인 경우 — 서버는 cano를 env 계좌번호로 채운다.
_QUOTE_SRC_WITH_ACCOUNT = _QUOTE_SRC.replace(
    "def inquire_price(env_dv: str,", "def inquire_price(env_dv: str, cano: str,"
)

_BALANCE_SRC = '''# [국내주식] 주문/계좌 > 주식잔고조회
API_URL = "/uapi/domestic-stock/v1/trading/inquire-balance"

def inquire_balance(env_dv: str, cano: str, acnt_prdt_cd: str):
    tr_id = "TTTC8434R"
    return ka._url_fetch(API_URL, tr_id, "", {"CANO": cano, "ACNT_PRDT_CD": acnt_prdt_cd})
'''


def _fake_upstream(root: Path, sources: dict[str, str], urls: dict[str, str] | None = None) -> Path:
    base = "https://github.com/koreainvestment/open-trading-api/tree/main/examples_llm/domestic_stock"
    for name, src in sources.items():
        path = root / "examples_llm" / "domestic_stock" / name / f"{name}.py"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(src, encoding="utf-8")
    config = root / "MCP" / "Kis Trading MCP" / "configs" / "domestic_stock.json"
    config.parent.mkdir(parents=True, exist_ok=True)
    apis = {name: {"github_url": (urls or {}).get(name, f"{base}/{name}")} for name in sources}
    config.write_text(json.dumps({"apis": apis}), encoding="utf-8")
    return root


_PUBLIC = frozenset({("domestic_stock", "inquire_price")})


def test_check_passes_when_upstream_matches_the_pinned_table(gen, tmp_path):
    root = _fake_upstream(tmp_path, {"inquire_price": _QUOTE_SRC, "inquire_balance": _BALANCE_SRC})
    pinned = gen.build(root, commit="a" * 40)

    _, problems = gen.check(root, "b" * 40, pinned, _PUBLIC)

    assert problems == []


def test_check_fails_when_upstream_adds_account_input_to_a_public_quote(gen, tmp_path):
    """#442 검증 조건: upstream 예제에서 공개 시세 TR에 계좌 입력이 생기면 검사가 실패한다.

    뮤테이션: ``check``에서 ``public_quote_violations`` 줄을 지우면 개인정보 경계 단정이 red,
    ``_account_input``이 ``None``만 돌려주게 하면 두 단정이 모두 red.
    """
    root = _fake_upstream(tmp_path, {"inquire_price": _QUOTE_SRC, "inquire_balance": _BALANCE_SRC})
    pinned = gen.build(root, commit="a" * 40)
    (root / "examples_llm" / "domestic_stock" / "inquire_price" / "inquire_price.py").write_text(
        _QUOTE_SRC_WITH_ACCOUNT, encoding="utf-8"
    )

    fresh, problems = gen.check(root, "b" * 40, pinned, _PUBLIC)

    assert "[개인정보 경계] inquire_price: 계좌·사용자 입력을 받는다 (CANO)" in problems
    assert "[판정표 drift] 바뀐 TR: inquire_price.account_input: None -> 'CANO'" in problems
    assert fresh["_meta"]["upstream_commit"] == "b" * 40


def test_check_fails_when_server_config_points_elsewhere(gen, tmp_path):
    """서버 설정이 다른 경로·브랜치의 예제를 가리키면 판정표가 서버가 실행하는 파일을 본 것이 아니다."""
    elsewhere = "https://github.com/koreainvestment/open-trading-api/tree/dev/examples_llm/domestic_stock/inquire_price"
    root = _fake_upstream(tmp_path, {"inquire_price": _QUOTE_SRC}, urls={"inquire_price": elsewhere})
    pinned = gen.build(root, commit="a" * 40)

    _, problems = gen.check(root, "a" * 40, pinned, _PUBLIC)

    assert len(problems) == 1 and problems[0].startswith("[서버 설정] inquire_price: github_url")
