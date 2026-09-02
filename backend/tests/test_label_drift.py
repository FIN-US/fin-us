"""TOOL_LABELS/AGENT_LABELS와 finus_nat 식별자의 드리프트 검사 (#282).

`backend/telegram_commands.py`의 두 라벨 맵은 **다른 서비스(finus_nat)의 식별자**를
키로 쓴다 — `TOOL_LABELS`는 도구 강제 원장에 기록되는 내부 도구명,
`AGENT_LABELS`는 supervisor 브랜치명이다. finus_nat이 도구를 리네임하면 backend에는
아무 신호도 가지 않고, 각주에 한국어 라벨 대신 내부 이름이 그대로 노출된다.
열화가 graceful해서(#260) 장애로 드러나지 않으므로 아무도 모른 채 오래간다.

**검사 방향**: finus_nat에 있는데 backend에 없는 키만 실패시킨다. backend에 남는 키(더
이상 존재하지 않는 도구·브랜치)는 각주 품질을 해치지 않으므로 실패시키지 않는다.
반대 방향까지 걸면 finus_nat 쪽 도구를 지우는 PR이 backend 수정을 강제로 끌고 들어온다.

**왜 backend 테스트인가**: 제약의 소유자가 backend이기 때문이다. "finus_nat의 식별자를
전부 덮어야 한다"는 요구는 backend 라벨 맵의 요구이고, 깨졌을 때 고칠 파일도 backend에
있다. finus_nat 쪽 잡에 두면 finus_nat만 건드린 PR이 backend 파일 때문에 빨간불이 되어
실패 지점과 수정 지점이 어긋난다. CI는 레포 전체를 체크아웃하므로 backend 잡에서
finus_nat 소스를 읽을 수 있고, 이미 같은 방식으로 서비스 경계를 넘는 테스트가 있다
(`test_stock_code.py`가 mcp-trading의 종목마스터를 읽는다). 새 잡을 만들 이유는 없다.

finus_nat 소스는 **정적으로만** 읽는다(ast/yaml). backend 잡에는 nvidia-nat 의존성이
없으므로 import는 불가능하고, 필요하지도 않다.
"""

import ast
from pathlib import Path

import pytest
import yaml

from backend.presentation import AGENT_LABELS, TOOL_LABELS

_REPO_ROOT = Path(__file__).resolve().parents[2]
_FINUS_NAT_ROOT = _REPO_ROOT / "finus_nat"
_SRC_ROOT = _FINUS_NAT_ROOT / "src"
_CONFIGS_ROOT = _FINUS_NAT_ROOT / "configs"

# 스캔 대상은 파일 목록이 아니라 **패턴**이다. 오늘 원장 기록은 finus_api.py 한 곳에만,
# branches는 router 두 파일에만 있지만, 그걸 상수로 박아 두면 새 모듈·새 라우터 config가
# 검사 밖에서 조용히 자란다 — 이 검사가 막으려던 것과 정확히 같은 실패 모드를 한 겹
# 안쪽에 다시 만드는 셈이다(1400줄짜리 finus_api.py가 분할되는 시점이 특히 그렇다).
# telegram_commands.py의 주석도 특정 파일이 아니라 router*.yml로 계약을 적어 뒀다.
# 두 목록이 비면 검사가 통째로 공허해지므로 아래 test_scan_inputs_are_discovered가 잡는다.
_SOURCE_FILES = sorted(_SRC_ROOT.rglob("*.py"))
_ROUTER_CONFIGS = sorted(_CONFIGS_ROOT.glob("router*.yml"))

# 원장 기록 함수와, 그 함수에 도구명을 흘려 보내는 래퍼의 키워드 인자.
# finus_api.py의 원격 MCP 호출 래퍼는 도구명을 자기 파라미터로 받아 원장 기록 함수에
# 그대로 넘긴다 — 그 경로의 실제 도구명은 호출부의 ledger_tool_name= 리터럴에만 있다.
#
# 진입점이 둘인 이유(#231): 도구가 결과를 돌려주는 자리는 이제 전부 `_record_and_mask`를
# 지난다(원장 기록 + PII 마스킹). `_record_to_ledger`는 그 안에서 한 번 불리는 하위
# 함수로 남았으므로, 도구명 리터럴은 `_record_and_mask` 호출부에 있다. 둘 다 훑어야
# 이 검사가 비지 않는다 — 하나만 두면 #282 드리프트 검사가 조용히 공허해진다.
_LEDGER_FUNCS = frozenset({"_record_to_ledger", "_record_and_mask"})
_LEDGER_FIRST_PARAM = "tool_name"
_LEDGER_KWARG = "ledger_tool_name"
# 래퍼가 "자기 파라미터를 그대로 전달"하는 자리의 이름들. 실제 값은 그 래퍼의 호출부에서
# 이미 수집되므로 미해석으로 세지 않는다. `tool_name`은 `_record_and_mask`가
# `_record_to_ledger`로 넘기는 자리다.
_FORWARDED_PARAM_NAMES = frozenset({_LEDGER_KWARG, _LEDGER_FIRST_PARAM})


def _module_level_str_constants(tree: ast.Module) -> dict[str, str]:
    """모듈 최상위의 문자열 상수 이름 → 값. 상수로 뽑아 쓴 도구명을 되짚는 데 쓴다."""
    constants: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets = [t for t in node.targets if isinstance(t, ast.Name)]
            value = node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            targets = [node.target]
            value = node.value
        else:
            continue
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            for target in targets:
                constants[target.id] = value.value
    return constants


def _ledger_tool_name_exprs(tree: ast.Module) -> list[ast.expr]:
    """도구명이 담기는 표현식을 전부 모은다 — _LEDGER_FUNCS 첫 인자 + ledger_tool_name=."""
    exprs: list[ast.expr] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name):
            called = func.id
        elif isinstance(func, ast.Attribute):
            called = func.attr
        else:
            called = None
        if called in _LEDGER_FUNCS:
            if node.args:
                exprs.append(node.args[0])
            else:
                # 첫 인자를 키워드로 넘긴 형태. 둘 다 없으면 아래 keywords 루프에도 걸리지
                # 않아 미해석으로 잡히지 못하므로, 호출 노드 자체를 넣어 드러나게 한다.
                by_kw = [kw.value for kw in node.keywords if kw.arg == _LEDGER_FIRST_PARAM]
                exprs.append(by_kw[0] if by_kw else node)
        exprs.extend(kw.value for kw in node.keywords if kw.arg == _LEDGER_KWARG)
    return exprs


def _extract_ledger_tool_names() -> tuple[set[str], list[str]]:
    """finus_nat/src 전체를 훑어 (도구명 집합, 정적으로 해석하지 못한 표현식 목록)을 낸다.

    상수는 파일별로 계산한다 — 모듈이 갈라져도 각자의 최상위 상수로 해석되고, 다른
    모듈의 동명 상수가 끼어들지 않는다. 남의 모듈에서 import해 온 이름은 미해석으로
    잡히는데, 그게 맞다: _record_to_ledger의 docstring이 "리터럴이나 모듈 상수로
    유지하라"를 계약으로 못박아 뒀고, 어긴 자리는 조용히 넘어가는 대신 빨간불이 된다.
    """
    names: set[str] = set()
    unresolved: list[str] = []
    for path in _SOURCE_FILES:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        exprs = _ledger_tool_name_exprs(tree)
        if not exprs:  # 원장을 건드리지 않는 모듈이 대부분이다
            continue
        constants = _module_level_str_constants(tree)
        for expr in exprs:
            if isinstance(expr, ast.Constant) and isinstance(expr.value, str):
                names.add(expr.value)
            elif isinstance(expr, ast.Name) and expr.id in constants:
                names.add(constants[expr.id])
            elif isinstance(expr, ast.Name) and expr.id in _FORWARDED_PARAM_NAMES:
                # 래퍼가 자기 파라미터를 그대로 전달하는 자리. 실제 값은 그 래퍼의
                # 호출부(ledger_tool_name= 리터럴, _record_and_mask 첫 인자)에서
                # 이미 수집된다.
                continue
            else:
                location = path.relative_to(_REPO_ROOT).as_posix()
                unresolved.append(f"{location}:{expr.lineno}: {ast.unparse(expr)}")
    return names, unresolved


def _config_chain(path: Path) -> list[Path]:
    """`base:` 상속 체인을 따라간 config 파일 목록(자기 자신 포함)."""
    chain: list[Path] = []
    seen: set[Path] = set()
    current: Path | None = path.resolve()
    while current is not None and current not in seen:
        assert current.exists(), f"finus_nat config가 없습니다: {current}"
        seen.add(current)
        chain.append(current)
        document = yaml.safe_load(current.read_text(encoding="utf-8")) or {}
        base = document.get("base") if isinstance(document, dict) else None
        current = (current.parent / base).resolve() if isinstance(base, str) else None
    return chain


def _extract_branch_names(path: Path) -> set[str]:
    """config(+`base:` 상속 체인) 안의 모든 `branches[].name`.

    supervisor 함수 이름에 의존하지 않고 재귀로 훑는다. 정의가 다른 키 아래로 옮겨가도
    계속 잡히고, 그래도 비면 호출부 테스트가 빨간불이 되므로 fail-open으로 무너지지 않는다.
    """
    names: set[str] = set()

    def walk(node: object) -> None:
        if isinstance(node, dict):
            branches = node.get("branches")
            if isinstance(branches, list):
                names.update(
                    branch["name"]
                    for branch in branches
                    if isinstance(branch, dict) and isinstance(branch.get("name"), str)
                )
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    for config_path in _config_chain(path):
        walk(yaml.safe_load(config_path.read_text(encoding="utf-8")) or {})
    return names


def test_scan_inputs_are_discovered():
    """스캔 입력이 비면 아래 검사들이 통째로 공허해지므로 목록부터 확인한다.

    router config 목록은 특히 조용하다 — 비면 parametrize가 빈 파라미터 집합이 되어
    커버리지 테스트가 실패가 아니라 skip으로 사라진다.
    """
    assert _SOURCE_FILES, f"finus_nat 소스를 하나도 찾지 못했습니다: {_SRC_ROOT}"
    assert _ROUTER_CONFIGS, f"router*.yml을 하나도 찾지 못했습니다: {_CONFIGS_ROOT}"


def test_every_ledger_tool_name_is_statically_resolvable():
    """도구명을 하나라도 정적으로 못 읽으면 아래 커버리지 검사가 공허해지므로 먼저 막는다."""
    names, unresolved = _extract_ledger_tool_names()
    assert unresolved == [], (
        "finus_nat 소스의 도구 원장 기록에서 도구명을 정적으로 해석하지 못했습니다. "
        "리터럴이나 모듈 상수로 되돌리거나, 이 파일의 추출 규칙을 함께 넓히세요 "
        f"(그대로 두면 #282 드리프트 검사가 조용히 비어 갑니다): {unresolved}"
    )
    assert names, (
        f"{_SRC_ROOT.name} 아래에서 도구명을 하나도 찾지 못했습니다 — "
        f"{sorted(_LEDGER_FUNCS)} 호출 규약이 바뀌었다면 추출 규칙을 함께 고쳐야 합니다."
    )


def test_ledger_tool_names_are_covered_by_tool_labels():
    """finus_nat이 원장에 기록하는 도구는 전부 TOOL_LABELS에 한국어 라벨이 있어야 한다."""
    names, _ = _extract_ledger_tool_names()
    missing = sorted(names - TOOL_LABELS.keys())
    assert missing == [], (
        "finus_nat이 원장에 기록하는 다음 도구가 backend TOOL_LABELS에 없습니다. "
        "각주에 한국어 라벨 대신 내부 이름이 노출됩니다 — "
        f"backend/presentation.py의 TOOL_LABELS에 추가하세요: {missing}"
    )


@pytest.mark.parametrize("config_path", _ROUTER_CONFIGS, ids=lambda p: p.name)
def test_router_branch_names_are_covered_by_agent_labels(config_path: Path):
    """router*.yml의 supervisor 브랜치는 전부 AGENT_LABELS에 한국어 라벨이 있어야 한다."""
    names = _extract_branch_names(config_path)
    assert names, (
        f"{config_path.name}에서 branches[].name을 하나도 찾지 못했습니다 — "
        "브랜치 정의 위치가 바뀌었다면 추출 규칙을 함께 고쳐야 합니다."
    )
    missing = sorted(names - AGENT_LABELS.keys())
    assert missing == [], (
        f"{config_path.name}의 다음 supervisor 브랜치가 backend AGENT_LABELS에 없습니다. "
        "각주에 한국어 라벨 대신 내부 이름이 노출됩니다 — "
        f"backend/presentation.py의 AGENT_LABELS에 추가하세요: {missing}"
    )
