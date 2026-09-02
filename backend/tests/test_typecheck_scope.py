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

_PYPROJECT_PATH = Path(__file__).resolve().parents[1] / "pyproject.toml"

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
    excludes = frozenset(_pyright_config()["exclude"])

    # 대칭차로 본다. 한쪽만 보면 기본 세 항목 중 하나가 **빠진** 경우에 실패 메시지가
    # 빈 목록이 되어 원인을 알려 주지 못한다 — 그것도 회귀다(`**/.*`가 빠지면 .venv가
    # 검사 대상이 되어 서드파티 오류가 쏟아지고, 그 소음을 잠재우려고 다시 파일을
    # 제외하게 된다).
    assert excludes == _DEFAULT_EXCLUDES, (
        "pyright exclude가 기본 세 항목과 다릅니다 "
        f"(더 있음={sorted(excludes - _DEFAULT_EXCLUDES)}, "
        f"빠짐={sorted(_DEFAULT_EXCLUDES - excludes)}). "
        "제외된 파일에 새로 쓰는 코드는 CI 타입 체크 잡이 보지 않습니다 — "
        "파일 단위 제외 대신 해당 줄의 `# pyright: ignore[규칙]`를 쓰세요."
    )


def test_pyright_silences_no_file_and_no_rule_wholesale():
    """`exclude` 말고도 같은 결과를 내는 두 가지 문을 함께 닫는다 (PR #337 리뷰).

    `exclude`만 고정해서는 장치가 닫히지 않는다. 결과가 같은 우회로가 둘 있다:

    - ``ignore = ["tests/test_config.py"]``는 파일을 파싱하되 진단을 전부 억제한다.
      잡은 초록불이고, 그 파일의 새 코드는 검사받지 않는다 — `exclude`와 구별되지
      않는 결과다. 게다가 설정 주석이 "파일 단위 제외 말고 줄 단위 ignore를 쓰라"고
      안내하므로 다음 사람이 손댈 자리가 오히려 이쪽이다.
    - ``reportArgumentType``처럼 규칙 하나를 전역으로 낮추는 것도 같다. #319가 걷어낸
      오류의 대부분이 reportArgumentType이었으니, 그 한 줄이면 이 PR 전체가 되돌려진다.

    둘 다 "정말 필요하면 그 줄에 `# pyright: ignore[규칙]`"라는 같은 대안을 갖는다.
    그쪽은 무엇을 왜 껐는지가 코드 옆에 남고, 그 줄만 꺼진다.

    규칙 쪽은 **끄는 철자를 열거하지 않고 켜는 철자를 고정한다** (PR #337 2차 리뷰).
    ``"none"``만 막으면 결과가 같은 두 철자가 그대로 남기 때문이다. 워크트리에서
    reportArgumentType을 유발하는 오류를 심고 한 줄씩 넣어 실측한 결과다:

        (없음)                            1 error   pyright exit 1  ← CI 빨간불
        reportArgumentType = "none"       0 errors  exit 0
        reportArgumentType = false        0 errors  exit 0
        reportArgumentType = "warning"    0 errors, 1 warning, exit 0

    마지막 것이 특히 눈에 띈다 — 진단은 남지만 CI 잡이 ``--warnings`` 없이 돌아
    그대로 초록불이다. 그러니 이 잡에게 "warning"과 "off"는 같은 말이고, 통과시킬
    값은 ``"error"``와 ``true`` 둘뿐이다. 규칙을 **조이는** 변경(basic이 끄고 있는
    규칙을 "error"로 켜는 것)은 그대로 통과한다 — 막아야 할 것은 푸는 방향이다.

    ``--warnings``를 CI에 붙여 warning도 빨간불로 만드는 길도 있다. 그러면 "warning"이
    실제 의미를 갖지만, basic 모드가 다른 곳에서 내는 warning까지 함께 빨간불이 되므로
    이 PR의 범위를 넘는다. 필요해지면 그때 이 단언과 함께 고칠 것.
    """
    config = _pyright_config()

    assert "ignore" not in config, (
        "pyright ignore 목록이 생겼습니다: "
        f"{config['ignore']}. exclude와 결과가 같습니다(진단이 전부 억제됩니다) — "
        "해당 줄의 `# pyright: ignore[규칙]`를 쓰세요."
    )

    # `value is not True`로 쓴다. `value != True`는 1도 통과시키는데, TOML에서 온
    # 정수 1은 pyright 설정으로 유효하지 않으므로 통과시킬 이유가 없다.
    loosened = sorted(
        f"{key} = {value!r}"
        for key, value in config.items()
        if key.startswith("report") and value != "error" and value is not True
    )
    assert not loosened, (
        f"전역으로 낮춘 진단 규칙이 있습니다: {loosened}. "
        "이 CI 잡은 --warnings 없이 돌므로 \"none\"·false·\"warning\"이 모두 같은 결과"
        "(초록불)입니다 — 규칙 하나를 레포 전체에서 낮추면 그 규칙이 지키던 주입 지점이 "
        "통째로 열립니다. 해당 줄의 `# pyright: ignore[규칙]`를 쓰세요."
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
