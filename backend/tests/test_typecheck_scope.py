"""pyright의 검사 범위가 backend/ 전체로 유지되는지 고정한다 (#319).

#292가 타입 체크 잡을 켤 때 네 개의 테스트 파일을 `exclude`에 남겼다(도입 시점 278건).
#319가 그 오류들을 고쳐 목록을 비웠지만, 목록이 다시 늘어나는 것을 막는 장치는
`backend/pyproject.toml`의 주석 한 줄뿐이었다 — 그리고 그 주석은 #292도 이미 달아
두었던 것이다.

제외의 대가는 조용하다. 잡은 초록불로 지나가고, 제외된 파일에 새로 들어온 코드가
검사를 받지 않았다는 사실은 아무 데도 드러나지 않는다. 실제로 #299 병합에서
`test_config.py`에 56줄이 한 줄도 검사되지 않은 채 들어왔고, 그것이 무해했다는 사실은
사람이 손으로 `exclude`를 풀고 재 보고서야 알았다.

그래서 여기서는 목록 자체를 고정한다. 파일을 다시 넣으려면 이 테스트를 함께 고쳐야
하므로, 제외는 조용한 기본값이 아니라 의식적인 행위가 된다. 개별 오류를 도저히
고칠 수 없다면 파일 단위 제외가 아니라 해당 줄의 `# pyright: ignore[규칙]`를 쓴다 —
그쪽은 무엇을 왜 껐는지가 코드 옆에 남고, 그 줄만 꺼진다.
"""

import tomllib
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PYPROJECT_PATH = _REPO_ROOT / "backend" / "pyproject.toml"

# 값을 주면 pyright의 내장 기본값이 대체되므로 설정이 함께 적어 두는 세 항목.
# 이것들은 소스 파일을 가리지 않는다 — 각각 서드파티, 바이트코드 캐시, 숨김 항목이다.
_DEFAULT_EXCLUDES = frozenset({"**/node_modules", "**/__pycache__", "**/.*"})


def _pyright_config() -> dict:
    return tomllib.loads(_PYPROJECT_PATH.read_text(encoding="utf-8"))["tool"]["pyright"]


def test_pyright_excludes_no_source_file():
    """`exclude`에 소스 파일이 없다.

    실패한다면 누군가 파일을 목록에 되돌려 놓았다는 뜻이고, 그 파일에 앞으로 쓰이는
    코드는 CI의 타입 체크 잡이 보지 않는다.
    """
    excludes = set(_pyright_config()["exclude"])

    assert excludes == set(_DEFAULT_EXCLUDES), (
        "pyright exclude에 기본 세 항목 외의 것이 들어 있습니다: "
        f"{sorted(excludes - _DEFAULT_EXCLUDES)}. "
        "제외된 파일에 새로 쓰는 코드는 CI 타입 체크 잡이 보지 않습니다 — "
        "파일 단위 제외 대신 해당 줄의 `# pyright: ignore[규칙]`를 쓰세요."
    )


def test_pyright_still_runs_in_basic_mode_over_the_repo_root():
    """검사 범위를 지탱하는 나머지 설정도 함께 고정한다.

    `exclude`만 비어 있어도 `extraPaths`가 빠지면 `backend.xxx` 절대 import가 전부
    풀리지 않아 오류가 import 실패로 뭉개지고, `venvPath`/`venv`가 빠지면 서드파티가
    전부 unresolved가 되어 같은 결과가 된다 — 목록을 비운 효과가 그 자리에서 사라진다.
    """
    config = _pyright_config()

    assert config["typeCheckingMode"] == "basic"
    assert config["extraPaths"] == [".."]
    assert (config["venvPath"], config["venv"]) == (".", ".venv")
