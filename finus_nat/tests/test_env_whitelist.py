"""NAT 경로 MCP 자식 프로세스 env 화이트리스트(_MCP_ENV_ALLOWED_PREFIXES) 테스트.

backend/config.py에 같은 목적의 화이트리스트가 별도로 존재한다(#130). 이 파일은
NAT 쪽 접두사 규칙만 검증하고, 마지막 테스트에서만 backend.config를 가져와 두
경로가 반드시 같이 움직여야 하는 최소 차원(KIS_ 네임스페이스)이 어긋나지
않는지 확인한다.
"""
import sys
from pathlib import Path

# backend와 finus_nat은 별도 uv 프로젝트(별도 venv)라 기본적으로 서로의 패키지를
# import할 수 없다. 마지막 테스트에서만 backend.config를 가져오기 위해 저장소
# 루트를 sys.path에 추가한다 — backend/config.py는 dotenv·mcp만 있으면 되고
# 둘 다 finus_nat venv에 이미 있다(nvidia-nat[mcp] 전이 의존성).
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import backend.config as backend_config  # noqa: E402
from nat_finus_nat.finus_api import _MCP_ENV_ALLOWED_PREFIXES, _mcp_child_env  # noqa: E402


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


def test_kis_namespace_prefix_is_present_on_both_backend_and_nat_paths():
    # #130의 핵심은 "같은 목적의 화이트리스트 두 벌이 어긋난다"는 것이었다.
    # 두 _MCP_ENV_ALLOWED_PREFIXES 튜플을 완전히 동일한지(==) 비교하지는
    # 않는다 — backend는 FINUS_*를 FINUS_KIS_로 의도적으로 좁혀 FINUS_MEM0_*
    # 등 backend/NAT 전용 변수가 새지 않게 막고, NAT는 이미 FINUS_ 전체를
    # 넓게 허용한다(finus_api.py가 FINUS_BACKEND_URL 등도 스스로 쓰기
    # 때문). 이 비대칭은 설계 의도이므로 완전 동일성 단언은 지금의 올바른
    # 상태에서도 실패해 신호가 아니라 잡음이 된다.
    #
    # 대신 두 경로가 반드시 같이 움직여야 하는 유일한 차원 — 자식 프로세스
    # (mcp-trading 등)가 공유하는 KIS_ 네임스페이스 자체 — 만 고정한다. 이후
    # 누군가 한쪽에서만 "KIS_"를 빼면 이 단언이 즉시 잡아낸다.
    assert "KIS_" in backend_config._MCP_ENV_ALLOWED_PREFIXES
    assert "KIS_" in _MCP_ENV_ALLOWED_PREFIXES
