"""NAT 경로 MCP 자식 프로세스 env 화이트리스트(_MCP_ENV_ALLOWED_PREFIXES) 테스트.

backend/config.py에 같은 목적의 화이트리스트가 별도로 존재한다(#130). 이 파일은
NAT 쪽 접두사 규칙만 검증하고, 마지막 테스트에서만 backend/config.py 소스를
정적으로 읽어(import하지 않는다) 두 경로가 반드시 같이 움직여야 하는 차원이
어긋나지 않는지 확인한다.
"""
import ast
from pathlib import Path

from nat_finus_nat.finus_api import _MCP_ENV_ALLOWED_PREFIXES, _mcp_child_env

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _prefix_tuple(path: Path, name: str) -> tuple[str, ...]:
    # backend와 finus_nat은 별도 uv 프로젝트(별도 venv)라 backend.config를
    # import하면 dotenv·mcp 같은 전이 의존성 여부(현재는 우연히 finus_nat
    # venv에도 있다)에 이 테스트 파일 전체의 수집(collection)이 묶이고,
    # backend/config.py:9-10의 load_dotenv()가 import 시점에 실행되어 NAT
    # 테스트 세션 전체의 os.environ을 개발자의 실제 저장소 루트 .env로
    # 오염시킨다(CI는 .env가 없어 이 회귀가 CI에서는 절대 드러나지 않는다).
    # 그래서 파일을 소스 텍스트로만 읽어 AST로 상수를 파싱한다 — 표준
    # 라이브러리만 쓰고, import도 side effect도 없다.
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == name for target in node.targets
        ):
            return ast.literal_eval(node.value)
    raise AssertionError(f"{name} not found in {path}")


def test_mcp_child_env_forwards_kis_tr_id_overrides(monkeypatch):
    # #130 — 이 수정 전에는 _MCP_ENV_ALLOWED_PREFIXES가 ("FIN_US_", "FINUS_")라
    # KIS_TR_ID_DAILY_CCLD/KIS_TR_ID_BALANCE_RLZ_PL이 둘 중 어느 접두사에도
    # 걸리지 않아 NAT 경로에서 조용히 빠졌다. NAT가 호출하는
    # get_today_daily_orders/get_balance_rlz_pl(finus_api.py)이 정확히 이
    # TR ID 오버라이드를 쓰는 도구들이라, 같은 .env로 backend 경로와 NAT
    # 경로가 다른 TR ID를 쓰는 상황이 발생했다.
    monkeypatch.setenv("KIS_TR_ID_DAILY_CCLD", "TTTC0081R")
    monkeypatch.setenv("KIS_TR_ID_BALANCE_RLZ_PL", "TTTC8494R")

    env = _mcp_child_env()

    assert env["KIS_TR_ID_DAILY_CCLD"] == "TTTC0081R"
    assert env["KIS_TR_ID_BALANCE_RLZ_PL"] == "TTTC8494R"


def test_mcp_child_env_still_forwards_finus_kis_overrides(monkeypatch):
    # 회귀 방지: 접두사 튜플에 "KIS_"를 추가한 것이 기존 "FINUS_" 매칭(FINUS_KIS_
    # TR ID 오버라이드 포함)을 깨서는 안 된다.
    monkeypatch.setenv("FINUS_KIS_TR_ID_DAILY_CCLD", "TTTC0081R")

    env = _mcp_child_env()

    assert env["FINUS_KIS_TR_ID_DAILY_CCLD"] == "TTTC0081R"


def test_mcp_child_env_does_not_forward_backend_only_secret(monkeypatch):
    # TELEGRAM_BOT_TOKEN: backend/config.py에 실재하는 값이고 mcp-trading·
    # mcp-news·mcp-dart 중 어느 것도 읽지 않는 backend 전용 비밀값이다
    # (backend/tests/test_config.py의 같은 단언과 동일 근거 — 임의의 더미가
    # 아니라 경계가 무너지면 실제로 아픈 값을 고른다). KIS_/FINUS_/FIN_US_
    # 어떤 접두사에도 걸리지 않으므로, 새로 넓힌 KIS_ 접두사가 여기까지
    # 새지 않는지 고정한다.
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "telegram-secret")

    env = _mcp_child_env()

    assert "TELEGRAM_BOT_TOKEN" not in env


def test_mcp_child_env_forwards_any_kis_prefixed_variable_by_mechanism(monkeypatch):
    # backend/tests/test_config.py의 KIS_FUTURE_VARIABLE_NOT_YET_INVENTED와 같은
    # 취지의 드리프트 방지 테스트. 존재한 적 없는 합성 변수명으로 "KIS_로
    # 시작하면 통과한다"는 접두사 매칭 메커니즘 자체를 고정한다 — 이름을
    # 나열하는 테스트라면 화이트리스트와 똑같은 방식으로 드리프트하지만, 이
    # 테스트는 누군가 _MCP_ENV_ALLOWED_PREFIXES에서 "KIS_"를 빼거나 개별 키
    # 나열로 되돌리면 이름 목록 갱신 없이도 즉시 실패한다.
    monkeypatch.setenv("KIS_FUTURE_VARIABLE_NOT_YET_INVENTED", "future-value")

    env = _mcp_child_env()

    assert env["KIS_FUTURE_VARIABLE_NOT_YET_INVENTED"] == "future-value"


def test_every_backend_prefix_is_subsumed_by_a_nat_prefix():
    # #130의 핵심은 "같은 목적의 화이트리스트 두 벌이 어긋난다"는 것이었다.
    # 두 _MCP_ENV_ALLOWED_PREFIXES 튜플을 완전히 동일한지(==) 비교하지는
    # 않는다 — backend는 FINUS_*를 FINUS_KIS_로 의도적으로 좁혀 FINUS_MEM0_*
    # 등 backend/NAT 전용 변수가 새지 않게 막고, NAT의 FINUS_는 이 PR 이전부터
    # 있던 기존 범위라 이번 변경의 대상이 아니다. 이 비대칭은 설계 의도이므로
    # 완전 동일성 단언은 지금의 올바른 상태에서도 실패해 신호가 아니라
    # 잡음이 된다.
    #
    # 대신 더 정확한 불변식을 쓴다: backend가 통과시키는 모든 접두사는 NAT
    # 접두사 중 하나로 시작해야 한다(포함 관계). 지금 상태에서 성립하는 이유는
    # 각 backend 접두사가 그대로 NAT에도 있기 때문이다(FIN_US_→FIN_US_,
    # FINUS_KIS_→FINUS_, KIS_→KIS_). 이 검사는 #130이 실제로 재발하는
    # 시나리오 — 누군가 backend에 새 접두사를 추가하고 NAT을 깜빡하는 것 —
    # 를 정확히 잡는다. 자기 자신과만 비교하는 "KIS_ in ..." 식의 단순 포함
    # 검사보다 엄격하다.
    backend_prefixes = _prefix_tuple(_REPO_ROOT / "backend" / "config.py", "_MCP_ENV_ALLOWED_PREFIXES")

    assert all(
        any(backend_prefix.startswith(nat_prefix) for nat_prefix in _MCP_ENV_ALLOWED_PREFIXES)
        for backend_prefix in backend_prefixes
    )
