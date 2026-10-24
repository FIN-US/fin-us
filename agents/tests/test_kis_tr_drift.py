"""#442: upstream ``main``과 판정표가 어긋나면 잡는 drift 검사의 오프라인 테스트.

Kis Trading MCP 서버는 도구 호출마다 upstream ``main``의 예제를 받아 실행하고, 버전을 고정하는
설정이 없다. 그래서 주기 워크플로(``.github/workflows/kis-upstream-drift.yml``)가
``agents/scripts/kis_tr_verdicts.py --check main``으로 판정표를 다시 만들어 대조한다. 네트워크는
그 워크플로에만 있고, 여기서는 받아 온 뒤의 판정·대조 로직을 가짜 upstream 트리로 본다.
"""

import copy
import importlib.util
import json
import urllib.error
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


# ---------------------------------------------------------------------------
# find_api_detail 설명 정정 표의 upstream 원문 대조 (#446)
# ---------------------------------------------------------------------------

def _with_description(root: Path, api_type: str, param: str, description: str) -> None:
    config = root / "MCP" / "Kis Trading MCP" / "configs" / "domestic_stock.json"
    data = json.loads(config.read_text(encoding="utf-8"))
    data["apis"][api_type]["params"] = {param: {"name": param, "description": description}}
    config.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


@pytest.mark.parametrize(("upstream_text", "flagged"), [
    ("순위 정렬 구분 코드 (0000: 등락률순)", False),
    ("순위 정렬 구분 코드 (0:상승율순 1:하락율순)", True),
])
def test_check_flags_when_upstream_changes_a_corrected_description(gen, tmp_path, upstream_text, flagged):
    """upstream이 정정 대상 설명을 바꾸면 [설명 정정]으로 알린다. 그대로면 조용하다.

    뮤테이션: ``check``에서 ``description_fix_mismatches`` 줄을 지우면 ``flagged=True`` 쪽 red.
    """
    from finus_agents.finus_api import _KIS_PARAM_DESCRIPTION_FIXES

    root = _fake_upstream(tmp_path, {"fluctuation": _QUOTE_SRC.replace("inquire_price", "fluctuation")})
    _with_description(root, "fluctuation", "fid_rank_sort_cls_code", upstream_text)
    pinned = gen.build(root, commit="a" * 40)

    _, problems = gen.check(root, "a" * 40, pinned, frozenset(), _KIS_PARAM_DESCRIPTION_FIXES)

    assert any(p.startswith("[설명 정정] fluctuation.fid_rank_sort_cls_code") for p in problems) is flagged


def test_main_passes_the_wrapper_description_fixes_to_check(gen, monkeypatch, tmp_path, capsys):
    """``--check``는 래퍼의 정정 표를 대조에 넘긴다 — 원문이 없는 설정이면 [설명 정정]이 뜬다."""
    _install_fake_fetch(gen, monkeypatch, tmp_path, {"inquire_price": _QUOTE_SRC, "inquire_balance": _BALANCE_SRC})
    monkeypatch.setattr(gen, "_description_fixes", lambda: {("domestic_stock", "inquire_price"): {
        "fid_input_iscd": type("Fix", (), {"upstream": "종목코드"})(),
    }})

    assert gen.main(["--check", "main"]) == 1
    assert "[설명 정정] inquire_price.fid_input_iscd" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# 종료 코드 — drift(1)와 upstream 받기 실패(2)를 가른다
# ---------------------------------------------------------------------------

def _install_fake_fetch(gen, monkeypatch, tmp_path, sources: dict[str, str]):
    """``fetch_upstream``을 가짜 트리 복사로 바꾸고, 고정 판정표를 원래 트리에서 만든다."""
    pinned_root = _fake_upstream(tmp_path / "pinned", {"inquire_price": _QUOTE_SRC, "inquire_balance": _BALANCE_SRC})
    pinned_path = tmp_path / "pinned.json"
    pinned_path.write_text(gen.render(gen.build(pinned_root, commit="a" * 40)), encoding="utf-8")
    monkeypatch.setattr(gen, "_PINNED_TABLE", pinned_path)
    monkeypatch.setattr(gen, "_public_quote_apis", lambda: _PUBLIC)
    # 가짜 설정에는 필드 설명이 없다. 설명 정정 대조는 아래 전용 테스트가 본다.
    monkeypatch.setattr(gen, "_description_fixes", lambda: {})

    def fake_fetch(ref, dest):
        _fake_upstream(dest, sources)
        return "b" * 40

    monkeypatch.setattr(gen, "fetch_upstream", fake_fetch)


def test_main_exits_0_without_drift(gen, monkeypatch, tmp_path):
    _install_fake_fetch(gen, monkeypatch, tmp_path, {"inquire_price": _QUOTE_SRC, "inquire_balance": _BALANCE_SRC})

    assert gen.main(["--check", "main"]) == 0


def test_main_exits_1_on_drift(gen, monkeypatch, tmp_path, capsys):
    _install_fake_fetch(gen, monkeypatch, tmp_path, {"inquire_price": _QUOTE_SRC_WITH_ACCOUNT,
                                                     "inquire_balance": _BALANCE_SRC})

    assert gen.main(["--check", "main"]) == 1
    assert "[개인정보 경계] inquire_price" in capsys.readouterr().out


@pytest.mark.parametrize("error", [
    urllib.error.URLError("연결 거부"),
    urllib.error.HTTPError("https://api.github.com/x", 404, "Not Found", None, None),
    TimeoutError("timed out"),
    json.JSONDecodeError("Expecting value", "", 0),
])
def test_main_exits_2_when_upstream_cannot_be_fetched(gen, monkeypatch, capsys, error):
    """받기 실패는 drift(1)가 아니라 2다 — "upstream이 바뀌었다"와 "닿지 못했다"를 가른다.

    뮤테이션: ``main``의 받기 실패 처리를 지우면 예외가 그대로 올라와 red, ``return 1``로 바꾸면 red.
    """
    def broken_fetch(ref, dest):
        raise error

    monkeypatch.setattr(gen, "fetch_upstream", broken_fetch)

    assert gen.main(["--check", "main"]) == 2
    assert "upstream 받기 실패" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# HTTP 재시도
# ---------------------------------------------------------------------------

class _Response:
    def __init__(self, body: bytes):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._body


def _scripted_urlopen(monkeypatch, gen, outcomes: list):
    calls = []

    def urlopen(request, timeout):
        calls.append(request)
        outcome = outcomes[len(calls) - 1]
        if isinstance(outcome, Exception):
            raise outcome
        return _Response(outcome)

    monkeypatch.setattr(gen.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(gen, "_sleep", lambda seconds: None)
    return calls


def _http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("https://raw.githubusercontent.com/x", code, "err", None, None)


@pytest.mark.parametrize("transient", [_http_error(503), urllib.error.URLError("reset"), TimeoutError("t")])
def test_http_get_retries_transient_errors(gen, monkeypatch, transient):
    """일시 오류는 다시 시도해 세 번째에 성공하면 그 응답을 돌려준다.

    뮤테이션: ``_HTTP_ATTEMPTS``를 1로 줄이면 red.
    """
    calls = _scripted_urlopen(monkeypatch, gen, [transient, transient, b"ok"])

    assert gen._http_get("https://raw.githubusercontent.com/x") == b"ok"
    assert len(calls) == 3


def test_http_get_gives_up_after_three_attempts(gen, monkeypatch):
    calls = _scripted_urlopen(monkeypatch, gen, [_http_error(502)] * 5)

    with pytest.raises(urllib.error.HTTPError):
        gen._http_get("https://raw.githubusercontent.com/x")
    assert len(calls) == 3


@pytest.mark.parametrize("code", [404, 422])
def test_http_get_does_not_retry_not_found_or_bad_ref(gen, monkeypatch, code):
    """없는 경로(404)·잘못된 ref(422)는 다시 보내도 같다 — 한 번만 보낸다.

    뮤테이션: ``_HTTP_NO_RETRY``를 비우면 red.
    """
    calls = _scripted_urlopen(monkeypatch, gen, [_http_error(code)] * 3)

    with pytest.raises(urllib.error.HTTPError):
        gen._http_get("https://raw.githubusercontent.com/x")
    assert len(calls) == 1


def test_token_goes_only_to_the_api_host_and_not_through_redirects(gen, monkeypatch):
    """토큰은 api.github.com 요청에만, 리다이렉트로 넘어가지 않는 헤더로 싣는다.

    뮤테이션: ``add_unredirected_header``를 ``add_header``로 바꾸면 red.
    """
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    calls = _scripted_urlopen(monkeypatch, gen, [b"{}", b"x"])

    gen._http_get("https://api.github.com/repos/x")
    gen._http_get("https://raw.githubusercontent.com/x")

    api, raw = calls
    assert api.unredirected_hdrs.get("Authorization") == "Bearer test-token"
    assert "Authorization" not in api.headers
    assert not raw.has_header("Authorization")
