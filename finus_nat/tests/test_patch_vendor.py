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
    """이미 적용된 트리에 다시 실행하면 already이고 파일이 더 바뀌지 않는다."""
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

    `apply_patch`를 읽어보면 루프 안에서는 `text`라는 로컬 변수만 갱신하고,
    `target.write_text(...)`는 루프를 다 돈 "뒤"에 딱 한 번만 호출한다(스크립트
    222~226행 주석 "여기서는 루프 밖에서 한 번만 쓴다" 참고). 3번째 치환에서
    old도 sentinel도 못 찾아 DRIFT를 조기 return하면, 그 return은 write_text
    호출 지점보다 앞에 있으므로 앞의 2개 치환이 로컬 변수에는 반영돼 있어도
    디스크에는 전혀 쓰이지 않는다.

    즉 이 구현은 "부분 적용을 조용히 디스크에 쓰는" 원본 run.sh의 108행 버그와는
    다르다. run.sh는 write_text를 세 번째 if 블록 안에 둬서, 세 번째 조건이
    거짓이면(이미 적용됐거나 매칭 안 되거나) 앞의 두 치환 결과를 담은 text 변수가
    아예 write_text 호출 자체를 못 만나 버려졌다 - 하지만 결과적으로 "조용히
    성공 종료"했다는 게 진짜 문제였다.

    새 구현의 결과 자체(파일 미변경)는 run.sh와 같지만, 성격이 다르다: 전부
    적용되거나 전혀 안 쓰이거나 둘 중 하나만 있는 all-or-nothing 원자적 쓰기이고,
    무엇보다 exit code 1 + DRIFT 상태로 "실패"를 크게 알린다. 파일을 절반만
    패치된 상태로 남기지 않는다는 점에서 안전하고, 원본 run.sh처럼 아무 신호 없이
    넘어가지도 않는다. 그래서 이 동작은 버그가 아니라 의도된 설계로 판단하고,
    "파일이 원본 그대로 유지된다"는 사실을 여기서 고정한다. (별도 위험 보고 없음.)
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

    sentinel 검사가 count 검사보다 먼저 실행돼야 하는 이유를 고정하는 순서 테스트다.
    순서가 바뀌면(count 검사가 먼저면), old가 우연히 두 번 있는 이미 적용된 트리가
    재실행 때마다 AMBIGUOUS로 잘못 실패하게 된다.
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
    """`shutil.copymode`의 방향은 target -> tmp다. 뒤집으면 읽기 전용 원본이
    tmp까지 읽기 전용으로 만들어 `os.replace()`를 막는다.
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
    테스트다. `os.replace`를 강제로 실패시켜, target이 원본 그대로 남고 tmp
    잔여물도 없는지 확인한다.
    """
    venv, site_packages = _make_site_packages(tmp_path)
    memory_patch = _patch_by_name(pv, MEMORY_NAME)
    target = _write_target(site_packages, memory_patch.parts, pv._MEMORY_OLD)
    original_bytes = target.read_bytes()

    def _raise(*_args, **_kwargs):
        raise OSError("simulated os.replace failure")

    monkeypatch.setattr(pv.os, "replace", _raise)

    with pytest.raises(OSError):
        pv.main(["--venv", str(venv)])

    assert target.read_bytes() == original_bytes
    assert list(target.parent.glob("*.tmp")) == []
