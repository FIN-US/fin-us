"""`finus_nat/scripts/patch_vendor.py` 동작 고정 테스트.

`patch_vendor.py`는 패키지가 아니라 독립 스크립트이므로 `importlib.util`로 파일
경로 기준으로 로드한다. 표준 라이브러리만 사용하므로 `nat`/`nat_finus_nat` 벤더
라이브러리 설치 여부와 무관하게 동작해야 한다 (이 파일은 절대 그 두 패키지를
import하지 않는다).

각 테스트는 `tmp_path`에 가짜 venv/site-packages 트리를 만들어 실제 파일 시스템
쓰기 동작(적용/멱등/drift/missing)을 고정한다.
"""

import importlib.util
import json
import os
import stat
import sys
from pathlib import Path

import pytest

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "patch_vendor.py"


def _load_patch_vendor():
    spec = importlib.util.spec_from_file_location("patch_vendor_under_test", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # patch_vendor.py uses `from __future__ import annotations` with dataclasses;
    # the dataclass decorator resolves string annotations via
    # `sys.modules[cls.__module__]`, so the module must be registered in
    # sys.modules *before* exec_module runs or it raises AttributeError.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def pv():
    return _load_patch_vendor()


def _make_site_packages(tmp_path: Path) -> tuple[Path, Path]:
    """posix 레이아웃(lib/python3.13/site-packages)의 가짜 venv를 만든다."""
    venv = tmp_path / "venv"
    site_packages = venv / "lib" / "python3.13" / "site-packages"
    site_packages.mkdir(parents=True)
    return venv, site_packages


def _write_target(site_packages: Path, parts: tuple[str, ...], content: str) -> Path:
    target = site_packages.joinpath(*parts)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return target


def _patch_by_name(pv, name: str):
    return next(p for p in pv.PATCHES if p.name == name)


def _read_marker(pv, site_packages: Path) -> dict:
    return json.loads((site_packages / pv.MARKER_FILENAME).read_text(encoding="utf-8"))


MEMORY_NAME = "nat/plugins/mem0ai/memory.py"
CLIENT_NAME = "mem0/client/main.py"
EDITOR_NAME = "nat/plugins/mem0ai/mem0_editor.py"
AGENT_NAME = "nat/plugins/langchain/agent/auto_memory_wrapper/agent.py"


def test_applies_patch_when_original_text_matches(pv, tmp_path):
    """원본 문자열이 그대로 있는 가짜 파일 -> new로 치환되고 exit code 0."""
    venv, site_packages = _make_site_packages(tmp_path)
    memory_patch = _patch_by_name(pv, MEMORY_NAME)
    target = _write_target(site_packages, memory_patch.parts, pv._MEMORY_OLD)

    exit_code = pv.main(["--venv", str(venv)])

    assert exit_code == 0
    assert target.read_text(encoding="utf-8") == pv._MEMORY_NEW

    marker = _read_marker(pv, site_packages)
    assert marker["patch_set_version"] == pv.PATCH_SET_VERSION
    assert marker["results"][MEMORY_NAME] == pv.APPLIED
    # 나머지 세 대상은 이 테스트에서 아예 만들지 않았으므로 missing으로 기록되고,
    # --require를 안 걸었으니 실패로 승격되지 않는다 (exit code는 여전히 0).
    assert marker["results"][CLIENT_NAME] == pv.MISSING
    assert marker["results"][EDITOR_NAME] == pv.MISSING
    assert marker["results"][AGENT_NAME] == pv.MISSING


def test_second_run_is_idempotent(pv, tmp_path):
    """이미 적용된 트리에 다시 실행하면 already이고 파일이 더 바뀌지 않는다.

    `apply_patch`가 sentinel 검사를 count 검사보다 먼저 하는 순서를 이 테스트가
    실질적으로 고정한다: 두 번째 실행 시점엔 `old`가 완전히 치환돼 사라졌으므로
    (count=0), 검사 순서가 뒤집히면(count 검사가 먼저면) sentinel을 보기도 전에
    `count == 0` 분기로 빠져 이 멀쩡한 재실행이 DRIFT로 오판된다. 즉 아래
    `second_exit == 0` / `ALREADY` 단언이 실패하면 그 순서 결정이 깨졌다는 뜻이다.
    """
    venv, site_packages = _make_site_packages(tmp_path)
    memory_patch = _patch_by_name(pv, MEMORY_NAME)
    target = _write_target(site_packages, memory_patch.parts, pv._MEMORY_OLD)

    first_exit = pv.main(["--venv", str(venv)])
    content_after_first = target.read_text(encoding="utf-8")

    second_exit = pv.main(["--venv", str(venv)])

    assert first_exit == 0
    assert second_exit == 0
    assert target.read_text(encoding="utf-8") == content_after_first
    assert content_after_first == pv._MEMORY_NEW

    marker = _read_marker(pv, site_packages)
    assert marker["results"][MEMORY_NAME] == pv.ALREADY


def test_drift_when_neither_original_nor_sentinel_present(pv, tmp_path):
    """파일은 있지만 old도 sentinel도 없으면 drift로 exit code 1 (issue #65 핵심).

    종전 run.sh는 `if old in text:` 형태라 이 상황에서 아무 것도 하지 않고
    조용히 성공 종료했다. 상류(mem0/NAT)가 코드를 바꿔서 old 리터럴이 더 이상
    일치하지 않으면, 패치가 실제로는 적용되지 않았는데도 빌드/배포가 성공한 것처럼
    보이는 게 문제였다. 새 구현은 이를 exit code 1로 드러내야 한다.
    """
    venv, site_packages = _make_site_packages(tmp_path)
    memory_patch = _patch_by_name(pv, MEMORY_NAME)
    drifted_content = "def get_client():\n    raise NotImplementedError('upstream rewritten')\n"
    _write_target(site_packages, memory_patch.parts, drifted_content)

    exit_code = pv.main(["--venv", str(venv)])

    assert exit_code == 1
    marker = _read_marker(pv, site_packages)
    assert marker["results"][MEMORY_NAME] == pv.DRIFT


def test_missing_target_warns_only_by_default(pv, tmp_path):
    """대상 파일이 아예 없으면 --require 없이는 경고만 하고 exit code 0."""
    venv, site_packages = _make_site_packages(tmp_path)

    exit_code = pv.main(["--venv", str(venv)])

    assert exit_code == 0
    marker = _read_marker(pv, site_packages)
    assert marker["results"][MEMORY_NAME] == pv.MISSING
    assert marker["results"][CLIENT_NAME] == pv.MISSING
    assert marker["results"][EDITOR_NAME] == pv.MISSING
    assert marker["results"][AGENT_NAME] == pv.MISSING


def test_missing_target_with_require_fails(pv, tmp_path):
    """없는 대상을 --require로 지정하면 exit code 1로 승격된다."""
    venv, _site_packages = _make_site_packages(tmp_path)

    exit_code = pv.main(["--venv", str(venv), "--require", MEMORY_NAME])

    assert exit_code == 1


def test_undefined_require_name_is_rejected(pv, tmp_path):
    """PATCHES에 정의되지 않은 이름을 --require로 주면 exit code 2 (인자 오류)."""
    venv = tmp_path / "venv"  # 검증이 site-packages 탐색보다 먼저 실행되므로 굳이 만들지 않아도 됨

    exit_code = pv.main(["--venv", str(venv), "--require", "nat/plugins/mem0ai/does_not_exist.py"])

    assert exit_code == 2


def test_marker_file_records_version_and_per_target_status(pv, tmp_path):
    """마커 파일(.finus_vendor_patch.json)에 patch_set_version과 대상별 상태가 담긴다."""
    venv, site_packages = _make_site_packages(tmp_path)
    memory_patch = _patch_by_name(pv, MEMORY_NAME)
    _write_target(site_packages, memory_patch.parts, pv._MEMORY_OLD)

    pv.main(["--venv", str(venv)])

    marker_path = site_packages / pv.MARKER_FILENAME
    assert marker_path.is_file()
    marker = json.loads(marker_path.read_text(encoding="utf-8"))
    assert marker["patch_set_version"] == pv.PATCH_SET_VERSION
    assert set(marker["results"].keys()) == {MEMORY_NAME, CLIENT_NAME, EDITOR_NAME, AGENT_NAME}
    assert marker["results"][MEMORY_NAME] == pv.APPLIED


def test_drift_in_third_replacement_leaves_earlier_matches_unwritten(pv, tmp_path):
    """mem0_editor.py처럼 치환이 3개인 파일에서 앞 2개는 매칭, 3번째만 drift인 경우.

    `apply_patch`는 루프 안에서 로컬 변수 `text`만 갱신하고 `_write_patched`는 루프
    밖에서 한 번만 부르므로, 3번째 치환에서 DRIFT로 조기 반환하면 앞 2개가 매칭됐어도
    디스크에는 전혀 쓰이지 않는다 - 그리고 `_write_patched` 자체도 tmp+os.replace로
    원자적이라 부분 쓰기가 없다.
    """
    venv, site_packages = _make_site_packages(tmp_path)
    editor_patch = _patch_by_name(pv, EDITOR_NAME)

    # 앞 2개(add_items 시그니처, metadata 호출)는 실제 old 리터럴과 정확히 일치시키고,
    # 3번째(search)는 sentinel도 old도 아닌 "상류가 바뀐" 모양으로 만든다.
    original_content = (
        "class MemoryEditor:\n"
        + pv._EDITOR_ADD_ITEMS_OLD
        + "        ...\n"
        + pv._EDITOR_METADATA_OLD
        + "\n"
        + "    async def search(self, query, top_k=10, **kwargs):\n"
        + '        user_id = kwargs.pop("user_id")  # upstream renamed this helper\n'
        + "        search_result = await self._client.search(query, user_id=user_id, top_k=top_k)\n"
    )
    target = _write_target(site_packages, editor_patch.parts, original_content)

    exit_code = pv.main(["--venv", str(venv)])

    assert exit_code == 1
    marker = _read_marker(pv, site_packages)
    assert marker["results"][EDITOR_NAME] == pv.DRIFT
    # 핵심 단언: 앞의 2개 치환이 매칭됐더라도 파일은 원본 그대로 남는다 (부분 쓰기 없음).
    assert target.read_text(encoding="utf-8") == original_content


def test_find_site_packages_locates_posix_layout(pv, tmp_path):
    """venv/lib/python3.13/site-packages 형태(posix)를 찾아낸다."""
    venv, site_packages = _make_site_packages(tmp_path)

    found = pv.find_site_packages(venv)

    assert found == site_packages


def test_find_site_packages_locates_windows_layout(pv, tmp_path):
    """venv/Lib/site-packages 형태(Windows)도 찾아낸다."""
    venv = tmp_path / "venv"
    site_packages = venv / "Lib" / "site-packages"
    site_packages.mkdir(parents=True)

    found = pv.find_site_packages(venv)

    assert found == site_packages


def test_conflict_when_sentinel_and_original_coexist(pv, tmp_path):
    """sentinel과 원본이 동시에 있으면 already가 아니라 conflict로 실패한다.

    sentinel은 앵커 없는 부분 문자열이라 상류가 우연히 같은 표현을 쓰면 오탐한다.
    그때 already로 넘어가면 패치가 안 붙은 채 exit 0으로 통과해, 이 스크립트가
    없애려던 "조용한 미적용"이 그대로 재현된다. 정상 적용 후에는 old가 치환돼
    사라지므로, 둘이 함께 있다는 것은 sentinel 오탐이라는 뜻이다.
    """
    venv, site_packages = _make_site_packages(tmp_path)
    memory_patch = _patch_by_name(pv, MEMORY_NAME)
    replacement = memory_patch.replacements[0]
    # 상류가 sentinel 문자열을 우연히 포함하면서 원본 블록도 그대로 가진 상황.
    target = _write_target(
        site_packages,
        memory_patch.parts,
        f'DEFAULT_KEY = "{replacement.sentinel}"\n\n{replacement.old}',
    )
    before = target.read_text(encoding="utf-8")

    exit_code = pv.main(["--venv", str(venv)])

    assert exit_code == 1
    marker = _read_marker(pv, site_packages)
    assert marker["results"][MEMORY_NAME] == pv.CONFLICT
    assert marker["ok"] is False
    # 오탐을 감지했을 뿐이므로 파일은 건드리지 않는다.
    assert target.read_text(encoding="utf-8") == before


def test_ambiguous_when_old_appears_twice_without_sentinel(pv, tmp_path):
    """old가 sentinel 없이 두 번 나타나면 AMBIGUOUS로 실패하고 파일은 그대로 남는다.

    `text.replace(old, new, 1)`은 첫 번째 발생만 바꾼다. 조용히 첫 번째만 바꾸고
    APPLIED로 보고하면, 두 번째 발생은 미적용인 채로 남는데도 exit 0으로 통과한다 -
    이 스크립트가 없애려던 바로 그 실패 양상이 패치 텍스트 레벨에서 재현된 것이다.
    """
    venv, site_packages = _make_site_packages(tmp_path)
    memory_patch = _patch_by_name(pv, MEMORY_NAME)
    replacement = memory_patch.replacements[0]
    duplicated = replacement.old + "\n# 상류가 같은 블록을 실수로 두 번 넣은 상황을 흉내낸다\n" + replacement.old
    target = _write_target(site_packages, memory_patch.parts, duplicated)
    before = target.read_text(encoding="utf-8")

    exit_code = pv.main(["--venv", str(venv)])

    assert exit_code == 1
    marker = _read_marker(pv, site_packages)
    assert marker["results"][MEMORY_NAME] == pv.AMBIGUOUS
    assert marker["ok"] is False
    assert target.read_text(encoding="utf-8") == before


def test_sentinel_and_duplicate_old_is_conflict_not_ambiguous(pv, tmp_path):
    """sentinel과 old(2회)가 동시에 있으면 CONFLICT다. AMBIGUOUS가 아니다.

    이 경우는 검사 순서와 무관하게 어느 쪽이든 exit 1이다 - 순서를 바꾸면 그냥
    라벨만 AMBIGUOUS로 바뀔 뿐 통과/실패가 갈리지 않는다. 그래서 이 테스트는
    "sentinel 우선 검사"라는 설계 결정 자체를 고정하는 게 아니라(그건
    `test_second_run_is_idempotent`가 한다), 이 특정 라벨(CONFLICT)이 유지되는지만
    본다.
    """
    venv, site_packages = _make_site_packages(tmp_path)
    memory_patch = _patch_by_name(pv, MEMORY_NAME)
    replacement = memory_patch.replacements[0]
    content = f'DEFAULT_KEY = "{replacement.sentinel}"\n\n{replacement.old}\n\n{replacement.old}'
    target = _write_target(site_packages, memory_patch.parts, content)

    exit_code = pv.main(["--venv", str(venv)])

    assert exit_code == 1
    marker = _read_marker(pv, site_packages)
    assert marker["results"][MEMORY_NAME] == pv.CONFLICT


def test_ambiguous_stays_ambiguous_on_rerun(pv, tmp_path):
    """AMBIGUOUS는 부분 적용 후 다음 실행에 다른 상태로 바뀌는 식이 아니라, 매 실행
    같은 상태(AMBIGUOUS)와 같은 바이트를 유지해야 한다 - 첫 실행이 조용히 첫
    발생만 바꿔놓고 그다음부터 다른 상태로 보이는 부분 적용은 없다.
    """
    venv, site_packages = _make_site_packages(tmp_path)
    memory_patch = _patch_by_name(pv, MEMORY_NAME)
    replacement = memory_patch.replacements[0]
    duplicated = replacement.old + "\n# duplicate\n" + replacement.old
    target = _write_target(site_packages, memory_patch.parts, duplicated)

    first_exit = pv.main(["--venv", str(venv)])
    content_after_first = target.read_text(encoding="utf-8")
    second_exit = pv.main(["--venv", str(venv)])
    content_after_second = target.read_text(encoding="utf-8")

    assert first_exit == 1
    assert second_exit == 1
    assert content_after_first == duplicated
    assert content_after_second == duplicated
    marker = _read_marker(pv, site_packages)
    assert marker["results"][MEMORY_NAME] == pv.AMBIGUOUS


def test_marker_records_ok_true_on_success(pv, tmp_path):
    """성공한 실행은 마커에 ok=True로 남는다.

    patch_set_version만 보고 "적용됨"으로 판단하면 실패한 실행의 마커를 정상으로
    오판한다. 소비하는 쪽이 ok를 함께 볼 수 있어야 한다.
    """
    venv, site_packages = _make_site_packages(tmp_path)
    memory_patch = _patch_by_name(pv, MEMORY_NAME)
    _write_target(site_packages, memory_patch.parts, pv._MEMORY_OLD)

    exit_code = pv.main(["--venv", str(venv)])

    assert exit_code == 0
    assert _read_marker(pv, site_packages)["ok"] is True


def test_mid_run_os_error_is_recorded_in_marker_and_returns_nonzero(pv, tmp_path, monkeypatch):
    """패치 루프 도중 한 대상의 쓰기에서 OSError(디스크 가득 참, 권한 문제 등)가
    나도 예외가 그냥 새지 않는다.

    각 파일 쓰기(`_write_patched`)는 원자적이지만 `main`의 반복 자체는 아니다:
    patch #1이 성공하고 patch #3의 쓰기가 실패하면, 예외가 그대로 전파될 경우
    `write_marker`가 아예 호출되지 않아 트리는 절반만 패치된 채 마커도 없이
    호출자에게 raw traceback만 남긴다. 이 PR이 만든 회귀는 아니지만(원자적 쓰기가
    생기기 전부터 있던 문제) 같은 파일에서 함께 고친다. 이 테스트는 실패한
    대상만 ERROR로 기록되고 나머지 대상은 계속 시도되며, 이미 성공한 대상(여기서는
    memory.py)의 쓰기 결과는 그대로 남는지 확인한다.
    """
    venv, site_packages = _make_site_packages(tmp_path)
    memory_patch = _patch_by_name(pv, MEMORY_NAME)
    editor_patch = _patch_by_name(pv, EDITOR_NAME)
    memory_target = _write_target(site_packages, memory_patch.parts, pv._MEMORY_OLD)
    editor_original_content = (
        "class MemoryEditor:\n"
        + pv._EDITOR_ADD_ITEMS_OLD
        + "        ...\n"
        + pv._EDITOR_METADATA_OLD
        + "\n"
        + "    async def search(self, query, top_k=10, **kwargs):\n"
        + pv._EDITOR_SEARCH_OLD
    )
    editor_target = _write_target(site_packages, editor_patch.parts, editor_original_content)

    real_replace = pv._replace

    def _flaky_replace(src, dst):
        if Path(dst) == editor_target:
            raise OSError("simulated disk full")
        return real_replace(src, dst)

    monkeypatch.setattr(pv, "_replace", _flaky_replace)

    exit_code = pv.main(["--venv", str(venv)])

    assert exit_code == 1
    marker = _read_marker(pv, site_packages)
    assert marker["ok"] is False
    assert marker["results"][MEMORY_NAME] == pv.APPLIED
    assert marker["results"][EDITOR_NAME] == pv.ERROR
    # memory.py 쓰기는 editor.py 쓰기 실패와 무관하게 이미 완료돼 있어야 한다 -
    # 한 대상의 오류가 이미 끝난 다른 대상의 결과를 되돌리지 않는다.
    assert memory_target.read_text(encoding="utf-8") == pv._MEMORY_NEW


def test_ambiguous_site_packages_is_rejected(pv, tmp_path):
    """site-packages 후보가 둘 이상이면 임의로 고르지 않고 실패한다.

    종전 Dockerfile이 `len(matches) != 1`로 강제하던 보증이다. 첫 후보를 말없이
    고르면 실제 인터프리터와 다른 트리를 패치하고도 성공으로 보고할 수 있다.
    """
    venv = tmp_path / "venv"
    (venv / "lib" / "python3.12" / "site-packages").mkdir(parents=True)
    (venv / "lib" / "python3.13" / "site-packages").mkdir(parents=True)

    with pytest.raises(pv.AmbiguousSitePackages):
        pv.find_site_packages(venv)

    assert pv.main(["--venv", str(venv)]) == 1


# ---------------------------------------------------------------------------
# _write_patched: hardlink 안전성 / 원자성 / LF 정규화 / 모드 보존
#
# LF·모드 테스트는 CI(ubuntu-latest)에서 신호가 0이다 - 개발자 로컬(특히 Windows)
# 회귀를 잡기 위한 가드일 뿐이다.
# ---------------------------------------------------------------------------


def _make_hardlinked_target(pv, tmp_path) -> tuple[Path, Path, Path]:
    """target과 inode를 공유하는 sibling(uv archive-v0 캐시 대역 흉내)을 만든다."""
    venv, site_packages = _make_site_packages(tmp_path)
    memory_patch = _patch_by_name(pv, MEMORY_NAME)
    target = _write_target(site_packages, memory_patch.parts, pv._MEMORY_OLD)
    sibling = tmp_path / "cache_sibling.py"
    try:
        os.link(target, sibling)
    except OSError:
        pytest.skip("이 파일시스템은 hardlink를 지원하지 않습니다 (os.link 실패).")
    return venv, target, sibling


def test_hardlink_sibling_content_untouched_after_write(pv, tmp_path):
    """target이 다른 경로와 inode를 공유해도, 패치 쓰기가 그 경로의 내용을 바꾸지
    않는다. `write_text()`가 공유 inode를 그대로 truncate+rewrite하던 시절에는
    sibling도 함께 바뀌었다 - 그게 실제로 uv archive-v0 캐시를 오염시킨 메커니즘이다.
    """
    venv, target, sibling = _make_hardlinked_target(pv, tmp_path)

    exit_code = pv.main(["--venv", str(venv)])

    assert exit_code == 0
    assert target.read_text(encoding="utf-8") == pv._MEMORY_NEW
    assert sibling.read_text(encoding="utf-8") == pv._MEMORY_OLD


def test_hardlink_inode_actually_breaks_after_write(pv, tmp_path):
    """내용 비교만으로는 "우연히 내용이 맞아떨어진 in-place 쓰기"와 "os.replace로
    경로를 진짜로 갈아치운 것"을 구분할 수 없다. inode 번호가 실제로 갈라졌는지까지
    확인해야 메커니즘 자체를 증명한 것이 된다.
    """
    venv, target, sibling = _make_hardlinked_target(pv, tmp_path)
    assert target.stat().st_ino == sibling.stat().st_ino  # sanity: hardlink 성립 확인

    exit_code = pv.main(["--venv", str(venv)])

    assert exit_code == 0
    assert target.stat().st_ino != sibling.stat().st_ino


def test_write_normalizes_to_lf_no_crlf_injected(pv, tmp_path):
    """`Path.write_text(..., encoding="utf-8")`는 `newline=None`이라 Windows에서
    매 `\\n`을 CRLF로 바꿔버린다(hardlink 여부와 무관하게 매 실행마다 발생).
    `_write_patched`는 `newline="\\n"`로 열어 LF를 유지해야 한다.
    """
    venv, site_packages = _make_site_packages(tmp_path)
    memory_patch = _patch_by_name(pv, MEMORY_NAME)
    target = site_packages.joinpath(*memory_patch.parts)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(pv._MEMORY_OLD.encode("utf-8"))

    exit_code = pv.main(["--venv", str(venv)])

    assert exit_code == 0
    assert b"\r\n" not in target.read_bytes()


@pytest.mark.skipif(os.name == "nt", reason="Windows는 POSIX 파일 모드 비트를 쓰지 않는다.")
def test_write_preserves_file_mode(pv, tmp_path):
    """`shutil.copymode`의 방향은 target -> tmp다. target의 원래 모드를 tmp에
    물려줘, replace 이후에도 벤더 파일의 모드가 보존되게 하기 위해서다(tmp는
    새로 만든 파일이라 물려줄 의미 있는 모드가 없다).
    """
    venv, site_packages = _make_site_packages(tmp_path)
    memory_patch = _patch_by_name(pv, MEMORY_NAME)
    target = _write_target(site_packages, memory_patch.parts, pv._MEMORY_OLD)
    target.chmod(0o640)

    exit_code = pv.main(["--venv", str(venv)])

    assert exit_code == 0
    assert stat.S_IMODE(target.stat().st_mode) == 0o640


def test_no_tmp_residue_after_applied_run(pv, tmp_path):
    """DRIFT 실행으로 이 테스트를 만들면 안 된다: `apply_patch`는 쓰기 *전에* DRIFT를
    반환하므로 `_write_patched`가 아예 호출되지 않고, 그러면 이 함수가 아무리
    tmp를 흘려도 테스트가 통과해버려 검증이 공허해진다. 반드시 실제 쓰기가 일어나는
    APPLIED 경로로 확인해야 한다.
    """
    venv, site_packages = _make_site_packages(tmp_path)
    memory_patch = _patch_by_name(pv, MEMORY_NAME)
    target = _write_target(site_packages, memory_patch.parts, pv._MEMORY_OLD)

    exit_code = pv.main(["--venv", str(venv)])

    assert exit_code == 0
    assert list(target.parent.glob("*.tmp")) == []


def test_replace_failure_leaves_target_intact_and_cleans_tmp(pv, tmp_path, monkeypatch):
    """`finally`의 `missing_ok=True` unlink가 실제로 동작하는지 검증하는 유일한
    테스트다. `_replace`를 강제로 실패시켜, target이 원본 그대로 남고 tmp
    잔여물도 없는지 확인한다.

    `apply_patch`를 직접 호출한다 - `main()`은 이 예외를 잡아 ERROR로 기록하고
    exit 1을 돌려주므로(`test_mid_run_os_error_is_recorded_in_marker_and_returns_nonzero`
    참고) 더는 여기까지 전파시키지 않는다. 이 테스트가 보는 건 그 아래 계층인
    `apply_patch`/`_write_patched`의 계약(예외를 삼키지 않고, tmp를 남기지 않는다)
    자체다.

    `pytest.raises(OSError)`만으로는 부족하다: `PermissionError`도 `OSError`의
    서브클래스라, `finally`의 unlink가 (읽기 전용 tmp 등으로) 두 번째
    `PermissionError`를 던져 원래 예외를 가려도 이 단언은 그냥 통과해버린다.
    그래서 예외 메시지까지 확인해 실제로 새어나온 게 시뮬레이션한 그 예외인지
    확정한다.
    """
    venv, site_packages = _make_site_packages(tmp_path)
    memory_patch = _patch_by_name(pv, MEMORY_NAME)
    target = _write_target(site_packages, memory_patch.parts, pv._MEMORY_OLD)
    # target을 읽기 전용으로 만들어야 `shutil.copymode(target, tmp)`가 그 모드를
    # tmp에 물려줘 tmp도 읽기 전용이 된다 - 그래야 finally의 unlink가 (Windows에서)
    # 실제로 PermissionError를 낼 수 있는 조건이 되어 그 예외를 삼키는 마스킹
    # 가드가 실질적으로 검증된다. 기본(쓰기 가능) 모드로 두면 tmp도 쓰기 가능해
    # unlink가 애초에 실패하지 않으므로, 가드를 없애도 이 테스트는 계속 통과해
    # 마스킹 수정이 실은 검증되지 않는 상태로 남는다.
    target.chmod(0o444)
    original_bytes = target.read_bytes()

    def _raise(*_args, **_kwargs):
        raise OSError("simulated os.replace failure")

    # `pv.os`는 실제 `os` 모듈이라 `os.replace`를 몽키패치하면 이 프로세스의 모든
    # 소비자(pytest 내부 포함)에 영향을 준다. `pv._replace`(patch_vendor.py 전용
    # 별칭)만 패치해 범위를 이 모듈 안으로 좁힌다.
    monkeypatch.setattr(pv, "_replace", _raise)

    try:
        with pytest.raises(OSError, match="simulated os.replace failure"):
            pv.apply_patch(site_packages, memory_patch)

        assert target.read_bytes() == original_bytes
        assert list(target.parent.glob("*.tmp")) == []
    finally:
        # 예외 경로에서 target은 원래 모드(0o444)로 복원된다(patch_vendor.py의
        # replace-실패 복원 로직). 일부 러너는 읽기 전용 파일이 남으면 tmp_path
        # 정리(teardown)에서 실패하므로 여기서 되돌린다.
        target.chmod(0o644)


def test_write_succeeds_when_target_is_read_only(pv, tmp_path):
    """target이 읽기 전용(0o444)이어도 `_write_patched`는 성공해야 한다.

    Windows에서는 `os.replace`(MoveFileEx)가 갈아치울 대상(target)이 읽기
    전용이면 거부한다 - 원본(tmp)의 모드는 상관없다(실측 확인됨). 그러면
    `os.replace` 자체가 `PermissionError`로 실패하고, `finally`의 unlink도 (tmp가
    copymode로 target의 읽기 전용 모드를 물려받았으므로) 같은 이유로 실패해
    원래 예외를 두 번째 `PermissionError`로 덮어쓰며 `.tmp` 잔여물까지
    site-packages 안에 남긴다. 이 테스트는 POSIX에서도 실행되긴 하지만(POSIX의
    rename(2)은 대상 파일의 권한 비트를 보지 않으므로 0o444가 `os.replace`를 막지
    않아 수정 전에도 통과한다), 실제 회귀 신호는 Windows에서만 나온다.
    """
    venv, site_packages = _make_site_packages(tmp_path)
    memory_patch = _patch_by_name(pv, MEMORY_NAME)
    target = _write_target(site_packages, memory_patch.parts, pv._MEMORY_OLD)
    target.chmod(0o444)

    try:
        exit_code = pv.main(["--venv", str(venv)])
        final_mode = stat.S_IMODE(target.stat().st_mode) if os.name != "nt" else None
    finally:
        # 일부 러너는 읽기 전용 파일이 남으면 tmp_path 정리(teardown)에서 실패한다.
        target.chmod(0o644)

    assert exit_code == 0
    assert target.read_text(encoding="utf-8") == pv._MEMORY_NEW
    assert list(target.parent.glob("*.tmp")) == []
    if final_mode is not None:
        # POSIX에서는 원래 모드(0o444)가 그대로 보존돼야 한다 - target이 아니라
        # target의 읽기 전용 비트만 잠깐 걷어냈다가 tmp(원래 모드를 copymode로
        # 물려받음)가 그 자리를 대신하므로 최종 모드는 원본과 같다.
        assert final_mode == 0o444


def test_target_writable_bit_is_cleared_before_replace_is_called(pv, tmp_path, monkeypatch):
    """`os.chmod(target, ...)`가 `_replace` 호출 *이전에* 실제로 target을 쓰기
    가능하게 만들었는지, 메커니즘 자체를 직접 검증한다.

    바로 위 `test_write_succeeds_when_target_is_read_only`는 최종 결과(성공,
    모드 보존)만 보는 행동 테스트라 Windows에서만 실질 신호가 있다 - POSIX의
    rename(2)은 대상의 권한 비트를 보지 않으므로, `os.chmod` 호출이 아예 없어도
    (즉 이 HIGH-2 수정이 통째로 없어도) 통과해버린다. CI는 ubuntu-latest뿐이라
    그 테스트만으로는 이 수정이 CI에서 한 번도 실행되지 않은 채로 green이다.

    `os.chmod`를 프로세스 전역으로 몽키패치하면 이 프로세스의 다른 소비자(pytest
    내부 포함)에도 영향을 준다. 대신 patch_vendor.py 전용 `_replace` 별칭
    seam(다른 테스트들이 이미 쓰는 좁은 접합부)을 스파이로 바꿔, `_replace`가
    호출되는 바로 그 시점에 target이 이미 쓰기 가능한 모드인지 `os.stat`으로
    직접 확인한다 - 플랫폼과 무관하게 메커니즘 자체를 고정하는 어서션이라
    POSIX(CI)에서도 신호가 있다.
    """
    venv, site_packages = _make_site_packages(tmp_path)
    memory_patch = _patch_by_name(pv, MEMORY_NAME)
    target = _write_target(site_packages, memory_patch.parts, pv._MEMORY_OLD)
    target.chmod(0o444)

    real_replace = pv._replace
    observed_modes: list[int] = []

    def _spy_replace(src, dst):
        # 이 시점에 target(dst)은 아직 원래 파일이다 - os.replace가 아직 안 불렸다.
        observed_modes.append(stat.S_IMODE(os.stat(dst).st_mode))
        return real_replace(src, dst)

    monkeypatch.setattr(pv, "_replace", _spy_replace)

    try:
        exit_code = pv.main(["--venv", str(venv)])
    finally:
        target.chmod(0o644)

    assert exit_code == 0
    assert len(observed_modes) == 1, "스파이를 거치지 않고 replace가 호출됐거나 여러 번 불렸다"
    # replace가 불리는 시점에는 target이 이미 쓰기 가능해야 한다 - chmod가
    # replace보다 먼저 실행됐다는 증거다. 원래 모드(0o444)에는 이 비트가 없었다.
    assert observed_modes[0] & stat.S_IWRITE
