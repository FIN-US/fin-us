#!/usr/bin/env python
"""NAT 벤더 라이브러리(site-packages) 패치 단일 진입점.

`Dockerfile`(빌드 타임)과 `scripts/run.sh`(로컬 실행)가 각각 site-packages를
텍스트 치환하던 것을 이 스크립트로 통합한다. 두 경로가 갈라지면서 Docker 이미지가
로컬보다 좁은 패치 집합만 받고 있었다(`mem0/client/main.py`, `mem0_editor.py`,
`auto_memory_wrapper/agent.py` 누락).

패치 대상은 전부 self-hosted Mem0 호환 처리다. NAT/mem0 상류가 클라우드 Mem0를
전제로 작성돼 있어, OSS self-host 엔드포인트에서 동작하도록 보정한다.

## 실패를 드러내는 방식

기존 `run.sh`는 `if old in text:` 형태라 상류 코드가 바뀌면 **조용히 아무것도 하지
않았다.** 이 스크립트는 다섯 상태를 구분한다.

- `already`   — sentinel이 이미 있음. 재실행해도 안전(멱등).
- `applied`   — 기대한 원본을 찾아 치환함.
- `drift`     — 파일은 있는데 sentinel도 원본도 없음. **상류가 바뀐 것이므로 실패로 처리한다.**
- `conflict`  — sentinel과 원본이 동시에 존재. sentinel 오탐이므로 실패로 처리한다.
- `ambiguous` — 원본 문자열(`old`)이 텍스트에 두 번 이상 나타남. `str.replace(..., 1)`은
                첫 번째만 바꾸므로, 두 번째 발생은 조용히 미적용으로 남을 수 있다.
                sentinel이 없는데 `old`가 둘 이상이면 실패로 처리한다.

## 종료 코드

- `0` — 모든 대상이 applied/already. `--require` 없는 missing도 여기 포함.
- `1` — drift·conflict·ambiguous가 하나라도 있거나, `--require` 대상이 없거나,
        site-packages를 못 찾았거나 후보가 모호함.
- `2` — 인자 오류 (정의되지 않은 `--require` 이름 등).

`missing`(대상 파일 없음)은 기본적으로 경고만 남긴다. 이 스크립트는 `--venv`로 어떤
venv든 받을 수 있어서, mem0ai/langchain extra가 아예 설치되지 않은 venv에 돌려도
빌드를 깨뜨리지 않아야 하기 때문이다. 반드시 있어야 하는 대상만 `--require`로
지정해 실패로 승격시킨다.

`finus_nat/pyproject.toml`은 `nvidia-nat[langchain,mcp,mem0ai]>=1.6.0,<2`를 필수
의존성으로 선언하므로, `uv sync --no-dev`로 만든 venv에는 네 대상이 전부 있어야
정상이다. `Dockerfile`은 그래서 네 파일 전부를 `--require`로 건다 — 하나라도
없으면(상류가 재배치했든, extra가 빠졌든) 빌드가 실패해야 그게 이 PR이 없애려는
"조용히 패치 안 된 이미지가 배포되는" 상황을 막는다. `--require`를 전부 걸지 않고
`memory.py` 하나만 강제하던 종전 방식은, 나머지 세 파일이 사라져도 빌드가 그냥
통과해버리는 바로 그 구멍이었다. CI의 벤더 패치 검증 스텝은 이미 네 개 전부를
걸고 있었으니, `Dockerfile`을 거기 맞춘 것뿐이다.

## 사용

    python finus_nat/scripts/patch_vendor.py --venv finus_nat/.venv
    python finus_nat/scripts/patch_vendor.py --venv /workspace/finus_nat/.venv \
        --require nat/plugins/mem0ai/memory.py

적용 결과는 site-packages의 `.finus_vendor_patch.json`에 남긴다. NAT 업그레이드 PR에서
`PATCH_SET_VERSION`과 이 마커를 비교하면 재적용 필요 여부를 사전에 감지할 수 있다.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import stat
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

# 패치 내용(old/new 리터럴)이 바뀔 때마다 올린다. 마커 파일과 비교해 재적용
# 필요 여부를 판단하는 소비자(예: `if marker < PATCH_SET_VERSION: 재적용`)가
# 있다면, 그 판단은 "치환 결과가 달라졌는가"에만 걸려야 한다.
#
# 이 PR로 스크립트 동작은 여러 군데 바뀌었지만(쓰기 경로가 hardlink-safe 원자적
# 쓰기로 교체, old 중복 발생을 감지하는 AMBIGUOUS 상태 추가, 읽기 전용 대상 쓰기
# 수정 등) old/new 리터럴 자체는 그대로라 v1이 만든 트리와 v2가 만들 트리는
# 바이트 단위로 동일하다 - 버전을 올릴 이유가 없다. 마커는 site-packages에
# 남고 오염 가능성이 있던 write_text() 경로는 `~/.cache/uv/archive-v0`에 있었으니,
# 마커의 버전 번호로 "이 트리가 오염된 캐시에서 왔는지"를 구분할 수도 없다 -
# 재실행으로 청소되는 대상이 애초에 아니다.
PATCH_SET_VERSION = 1

MARKER_FILENAME = ".finus_vendor_patch.json"


@dataclass(frozen=True)
class Replacement:
    """치환 한 건.

    sentinel: 이미 적용됐는지 판별하는 짧은 표식. 치환 결과에만 나타나야 한다.
              문구를 다듬어 `new`가 바뀌어도 sentinel이 유지되면 기존 venv를 drift로
              오판하지 않는다.
    """

    sentinel: str
    old: str
    new: str


@dataclass(frozen=True)
class VendorPatch:
    """site-packages 하위 파일 하나에 대한 패치 묶음."""

    parts: tuple[str, ...]
    reason: str
    replacements: tuple[Replacement, ...] = field(default=())

    @property
    def name(self) -> str:
        return "/".join(self.parts)


# ---------------------------------------------------------------------------
# 패치 정의
#
# 문자열은 상류 소스와 바이트 단위로 일치해야 하므로, 기존 run.sh/Dockerfile의
# 리터럴을 그대로 옮겼다. 임의로 줄바꿈·들여쓰기를 정리하지 말 것.
# ---------------------------------------------------------------------------

_MEMORY_OLD = """    mem0_api_key = os.environ.get("MEM0_API_KEY")\n\n    if mem0_api_key is None:\n        raise RuntimeError("Mem0 API key is not set. Please specify it in the environment variable 'MEM0_API_KEY'.")\n\n    mem0_client = AsyncMemoryClient(api_key=mem0_api_key,\n                                    host=config.host,\n                                    org_id=config.org_id,\n                                    project_id=config.project_id)\n"""

_MEMORY_NEW = """    mem0_api_key = os.environ.get("MEM0_API_KEY")\n\n    if mem0_api_key is None:\n        if config.host:\n            # Self-hosted Mem0: keep client init deterministic without env provisioning.\n            mem0_api_key = "selfhost-mem0-static-key"\n        else:\n            raise RuntimeError("Mem0 API key is not set. Please specify it in the environment variable 'MEM0_API_KEY'.")\n\n    if config.host:\n        # Open-source self-hosted Mem0 does not expose /v1/ping; bypass cloud-style API key validation.\n        original_validate = AsyncMemoryClient._validate_api_key\n\n        def _skip_validate(self):\n            return "selfhost-user"\n\n        AsyncMemoryClient._validate_api_key = _skip_validate\n        try:\n            mem0_client = AsyncMemoryClient(api_key=mem0_api_key,\n                                            host=config.host,\n                                            org_id=config.org_id,\n                                            project_id=config.project_id)\n        finally:\n            AsyncMemoryClient._validate_api_key = original_validate\n\n        mem0_client.async_client.headers.pop("Authorization", None)\n        mem0_client.async_client.headers.pop("Mem0-User-ID", None)\n    else:\n        mem0_client = AsyncMemoryClient(api_key=mem0_api_key,\n                                        host=config.host,\n                                        org_id=config.org_id,\n                                        project_id=config.project_id)\n"""

_CLIENT_ADD_OLD = """        response = await self.async_client.post("/v1/memories/", json=payload)\n"""
_CLIENT_ADD_NEW = """        endpoint = "/memories" if self.host and "api.mem0.ai" not in self.host else "/v1/memories/"\n        response = await self.async_client.post(endpoint, json=payload)\n"""

_CLIENT_SEARCH_OLD = """        response = await self.async_client.post(f"/{version}/memories/search/", json=payload)\n"""
_CLIENT_SEARCH_NEW = """        endpoint = "/search" if self.host and "api.mem0.ai" not in self.host else f"/{version}/memories/search/"\n        response = await self.async_client.post(endpoint, json=payload)\n"""

_EDITOR_ADD_ITEMS_OLD = """    async def add_items(self, items: list[MemoryItem]) -> None:\n"""
_EDITOR_ADD_ITEMS_NEW = """    async def add_items(self, items: list[MemoryItem], **kwargs) -> None:\n"""

_EDITOR_METADATA_OLD = """                                 metadata=item_meta,\n                                 output_format="v1.1"))\n"""
_EDITOR_METADATA_NEW = """                                 metadata=item_meta,\n                                 output_format="v1.1",\n                                 **kwargs))\n"""

_EDITOR_SEARCH_OLD = """        user_id = kwargs.pop("user_id")  # Ensure user ID is in keyword arguments\n\n        search_result = await self._client.search(query, user_id=user_id, top_k=top_k, output_format="v1.1", **kwargs)\n"""
_EDITOR_SEARCH_NEW = """        user_id = kwargs.pop("user_id")  # Ensure user ID is in keyword arguments\n        search_kwargs = dict(kwargs)\n\n        host = getattr(self._client, "host", "") or ""\n        if "api.mem0.ai" in host:\n            search_kwargs["user_id"] = user_id\n        else:\n            search_kwargs["filters"] = {"user_id": user_id}\n\n        try:\n            search_result = await self._client.search(query, top_k=top_k, output_format="v1.1", **search_kwargs)\n        except Exception:\n            if "api.mem0.ai" in host:\n                raise\n\n            response = await self._client.async_client.get("/memories", params={"user_id": user_id})\n            response.raise_for_status()\n            results = response.json().get("results", [])\n            needle = query.casefold().strip()\n            if needle:\n                matched = [item for item in results if needle in str(item.get("memory", "")).casefold()]\n                if matched:\n                    results = matched\n            search_result = {"results": results[:top_k]}\n"""

_AGENT_USER_MANAGER_OLD = """        user_manager = self._context.user_manager\n"""
_AGENT_USER_MANAGER_NEW = """        user_manager = getattr(self._context, "user_manager", None)\n"""

_AGENT_HEADERS_OLD = """        if self._context.metadata and self._context.metadata.headers:\n            user_id = self._context.metadata.headers.get("x-user-id")\n"""
_AGENT_HEADERS_NEW = """        metadata = getattr(self._context, "metadata", None)\n        headers = getattr(metadata, "headers", None)\n        if headers:\n            user_id = headers.get("x-user-id")\n"""


PATCHES: tuple[VendorPatch, ...] = (
    VendorPatch(
        parts=("nat", "plugins", "mem0ai", "memory.py"),
        reason="self-hosted Mem0는 /v1/ping을 제공하지 않아 클라우드식 API 키 검증을 우회해야 한다.",
        replacements=(
            Replacement(
                sentinel="selfhost-mem0-static-key",
                old=_MEMORY_OLD,
                new=_MEMORY_NEW,
            ),
        ),
    ),
    VendorPatch(
        parts=("mem0", "client", "main.py"),
        reason="OSS Mem0는 /memories·/search를 쓰고 클라우드는 /v1/... 을 쓴다. host로 분기한다.",
        replacements=(
            Replacement(
                sentinel='endpoint = "/memories" if self.host',
                old=_CLIENT_ADD_OLD,
                new=_CLIENT_ADD_NEW,
            ),
            Replacement(
                sentinel='endpoint = "/search" if self.host',
                old=_CLIENT_SEARCH_OLD,
                new=_CLIENT_SEARCH_NEW,
            ),
        ),
    ),
    VendorPatch(
        parts=("nat", "plugins", "mem0ai", "mem0_editor.py"),
        reason="OSS Mem0는 user_id 대신 filters를 쓰고 검색 실패 시 폴백이 필요하다.",
        replacements=(
            Replacement(
                sentinel="async def add_items(self, items: list[MemoryItem], **kwargs)",
                old=_EDITOR_ADD_ITEMS_OLD,
                new=_EDITOR_ADD_ITEMS_NEW,
            ),
            Replacement(
                sentinel='output_format="v1.1",\n                                 **kwargs))',
                old=_EDITOR_METADATA_OLD,
                new=_EDITOR_METADATA_NEW,
            ),
            Replacement(
                sentinel="search_kwargs = dict(kwargs)",
                old=_EDITOR_SEARCH_OLD,
                new=_EDITOR_SEARCH_NEW,
            ),
        ),
    ),
    VendorPatch(
        parts=("nat", "plugins", "langchain", "agent", "auto_memory_wrapper", "agent.py"),
        reason="NAT context에 user_manager·metadata가 없는 실행 경로가 있어 방어적으로 접근한다.",
        replacements=(
            Replacement(
                sentinel='user_manager = getattr(self._context, "user_manager", None)',
                old=_AGENT_USER_MANAGER_OLD,
                new=_AGENT_USER_MANAGER_NEW,
            ),
            Replacement(
                sentinel='headers = getattr(metadata, "headers", None)',
                old=_AGENT_HEADERS_OLD,
                new=_AGENT_HEADERS_NEW,
            ),
        ),
    ),
)


# ---------------------------------------------------------------------------
# 적용
# ---------------------------------------------------------------------------

APPLIED = "applied"
ALREADY = "already"
MISSING = "missing"
DRIFT = "drift"
CONFLICT = "conflict"
AMBIGUOUS = "ambiguous"
ERROR = "error"  # apply_patch가 아니라 OS 쓰기 자체가 실패함 (디스크 가득 참, 권한 등)

# `os.replace`를 직접 부르지 않고 이 모듈 전용 별칭을 거친다. 테스트가 실패를
# 시뮬레이션하려고 `os.replace`를 몽키패치하면 `os` 자체가 공유 모듈이라 이
# 프로세스의 모든 소비자(pytest 내부 포함)에 영향을 준다 - 테스트가 끝나면
# 되돌려지긴 하지만, 그동안 다른 코드가 `os.replace`를 부르면 오작동한다.
# `_replace`는 이 모듈 하나만 참조하는 좁은 접합부라 몽키패치 범위가 그만큼
# 좁아진다.
_replace = os.replace


class AmbiguousSitePackages(RuntimeError):
    """venv 안에 site-packages 후보가 둘 이상이다."""


def find_site_packages(venv: Path) -> Path | None:
    """venv 안의 site-packages를 찾는다. posix/windows 레이아웃을 모두 본다.

    후보가 둘 이상이면 고르지 않고 예외를 던진다. 종전 Dockerfile이
    `len(matches) != 1` 로 강제하던 보증을 유지하기 위해서다. 임의로 첫 번째를
    고르면 실제 인터프리터와 다른 트리를 패치하고도 성공으로 보고할 수 있다.
    """
    candidates = [
        candidate
        for candidate in (
            sorted(venv.glob("lib/python3.*/site-packages"))
            + sorted(venv.glob("Lib/site-packages"))
        )
        if candidate.is_dir()
    ]
    if len(candidates) > 1:
        raise AmbiguousSitePackages(
            f"site-packages 후보가 여러 개입니다: {[str(c) for c in candidates]}. "
            "어느 것이 실제 인터프리터의 것인지 확정할 수 없어 중단합니다."
        )
    return candidates[0] if candidates else None


def _write_patched(target: Path, text: str) -> None:
    """`target`을 site-packages 안에서 hardlink-safe하게, 원자적으로 덮어쓴다.

    uv 기본 link mode는 `hardlink`라 site-packages의 파일이 `~/.cache/uv/archive-v0/...`와
    같은 inode를 공유할 수 있다(`st_nlink` >= 2). 그 상태에서 `Path.write_text()`로 그냥
    덮어쓰면 inode를 그대로 truncate+rewrite하므로 uv의 전역 캐시까지 오염된다(실측:
    `st_nlink=2`, 캐시 엔트리가 그 자리에서 바뀜). 그래서 여기서는 같은 디렉터리에 새
    임시 파일을 만들고 `os.replace()`로 경로만 갈아치운다 — 원래 inode는 캐시 쪽에
    그대로 남고 `target` 경로만 새 inode를 가리키게 된다.

    주의: 이 함수는 줄바꿈을 LF로 **정규화**한다. 원본의 CRLF/LF를 그대로 보존하는 게
    아니다. `newline="\\n"`로 열기 때문에 `text`에 있는 `\\n`은 항상 LF 그대로 쓰인다
    (반대로 `write_text(..., encoding="utf-8")`는 `newline=None`이라 매 `\\n`을 OS
    개행으로 바꾸는데, Windows에서는 그게 CRLF라 매 실행마다 파일 전체가 CRLF로 재작성된다).
    `read_text()`가 `newline=None`이라 읽을 때 CRLF -> LF로 이미 접혀 있고, 이 스크립트가
    아는 네 대상은 pin된 버전에서 전부 순수 LF라 지금은 실질적 영향이 없다. 다음에 정말
    CRLF인 업스트림 대상을 추가하는 사람은 이 함수가 그것도 LF로 바꿔버린다는 걸 알아야 한다.
    """
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=target.name + ".", suffix=".tmp")
    try:
        # mkstemp는 이미 열린 OS 레벨 fd를 준다. 이름으로 다시 열지 않고 그 fd를
        # 그대로 소비해야 한다 — 그러지 않으면 fd가 열린 채 남아 Windows에서
        # os.replace()가 매번 PermissionError로 실패한다.
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        shutil.copymode(target, tmp)  # 방향: target -> tmp. target의 원래 모드(예:
        # 0o444)를 tmp에 물려줘, 아래 replace 이후에도 벤더 파일의 모드가 보존되게
        # 한다(반대 방향 copymode(tmp, target)는 이 목적에 맞지 않는다 - tmp는 새로
        # 만든 파일이라 물려줄 의미 있는 모드가 없고, target을 tmp의 기본 모드로
        # 덮어써 원래 모드를 잃는다).
        #
        # target 자체가 읽기 전용(예: 0o444)이면 Windows의 MoveFileEx(os.replace 내부
        # 구현)가 그 자리를 갈아치우길 거부해 PermissionError를 낸다(실측: 0o444
        # 대상에서 재현됨. os.chmod(tmp, ...)로 tmp만 쓰기 가능하게 만들어도 target이
        # 여전히 읽기 전용이면 실패는 그대로다 - 막는 쪽은 새로 들어올 tmp가 아니라
        # 갈아치워질 target이다). 그래서 target의 읽기 전용 비트만 잠깐 걷어내
        # replace가 통과하게 한다. tmp의 모드는 건드리지 않는다 - 위 copymode가 이미
        # target의 원래 모드를 tmp에 옮겨뒀고, replace가 성공하면 그 tmp가 target
        # 자리를 차지하므로 최종 모드는 굳이 다시 복원하지 않아도 원래 값(0o444)
        # 그대로 남는다.
        os.chmod(target, stat.S_IWRITE | os.stat(target).st_mode)
        _replace(tmp, target)
    finally:
        # 성공 경로에서는 os.replace()가 이미 tmp를 target 자리로 옮겨서 tmp가 더 이상
        # 존재하지 않는다. missing_ok=True 없이 무조건 unlink하면 그 성공을
        # FileNotFoundError로 뒤집어버린다.
        #
        # os.replace()가 실패하는 경우(예: 위 chmod로도 못 피한 다른 권한 문제)
        # tmp는 여전히 읽기 전용일 수 있어 이 unlink 자체도 PermissionError를 낼 수
        # 있다. try 블록의 진짜 예외가 finally에서 발생한 두 번째 예외로 덮이면
        # 호출자는 원인을 영영 알 수 없게 된다 - 정리 실패가 원본 실패를 가려서는
        # 안 된다.
        try:
            Path(tmp).unlink(missing_ok=True)
        except OSError:
            pass


def apply_patch(site_packages: Path, patch: VendorPatch) -> tuple[str, str]:
    """패치 하나를 적용하고 (상태, 메시지)를 돌려준다. 파일 쓰기는 한 번만 한다."""
    target = site_packages.joinpath(*patch.parts)
    if not target.is_file():
        return MISSING, f"대상 없음: {target}"

    text = original = target.read_text(encoding="utf-8")
    applied: list[str] = []
    already: list[str] = []

    for index, replacement in enumerate(patch.replacements):
        if replacement.sentinel in text:
            # sentinel은 앵커 없는 부분 문자열이라 상류가 우연히 같은 표현을 도입하면
            # 오탐할 수 있다. 정상 적용됐다면 old는 치환돼 사라졌어야 하므로, 둘이
            # 함께 있으면 sentinel 오탐이다. 이걸 걸러내지 않으면 미적용을 already로
            # 오판해 조용히 통과시킨다 — 이 스크립트가 없애려던 바로 그 실패 양상이다.
            if replacement.old in text:
                return CONFLICT, (
                    f"{patch.name} 치환 #{index}: 적용 표식과 원본이 동시에 존재합니다. "
                    f"sentinel 오탐으로 보입니다. sentinel={replacement.sentinel!r}"
                )
            already.append(str(index))
            continue
        # sentinel 검사가 먼저 끝나 있어야 한다: 정상적으로 이미 패치된 트리를
        # 재실행하는 흔한 경우(sentinel 있음, old는 치환돼 사라져 count=0)를 생각해
        # 보면, count 검사를 먼저 하는 순서에서는 sentinel을 보기도 전에
        # `count == 0` 분기로 빠져 멀쩡한 재실행이 매번 DRIFT로 오판된다(멱등
        # 재실행이 전부 실패로 뒤집힌다 - test_second_run_is_idempotent가 이 순서를
        # 고정한다). sentinel 분기에서 이미 already/conflict로 반환됐으므로, 여기
        # 도달했다는 것은 sentinel이 없다는 뜻이고 count만으로 판단해도 된다.
        count = text.count(replacement.old)
        if count == 0:
            # 상류가 바뀌었다. 조용히 넘어가면 런타임에 원인 불명으로 실패한다.
            return DRIFT, (
                f"{patch.name} 치환 #{index}: 기대한 원본을 찾지 못했고 적용 표식도 없습니다. "
                f"NAT/mem0 상류가 바뀐 것으로 보입니다. sentinel={replacement.sentinel!r}"
            )
        if count > 1:
            # text.replace(old, new, 1)은 첫 번째 발생만 바꾼다. old가 두 번 이상
            # 나타나면 어느 쪽이 실제로 바꿔야 할 자리인지 이 스크립트는 알 수 없다.
            # 그냥 첫 번째를 바꾸면 두 번째 자리는 조용히 미적용으로 남는데, 그게
            # 바로 이 스크립트가 없애려던 실패 양상이다. 자동으로 고르지 않고 실패시킨다.
            return AMBIGUOUS, (
                f"{patch.name} 치환 #{index}: 원본 문자열이 {count}번 나타나 어느 자리를 "
                f"바꿔야 할지 확정할 수 없습니다. old에 주변 문맥을 추가해 유일한 문자열이 "
                f"되도록 좁히세요. sentinel={replacement.sentinel!r}"
            )
        text = text.replace(replacement.old, replacement.new, 1)
        applied.append(str(index))

    # run.sh는 마지막 치환 블록 안에서만 write_text를 호출해, 앞선 치환이 조용히
    # 버려질 수 있었다. 여기서는 루프 밖에서 한 번만 쓴다.
    if text != original:
        _write_patched(target, text)
        return APPLIED, f"{patch.name}: 치환 {len(applied)}건 적용, {len(already)}건 기적용"

    return ALREADY, f"{patch.name}: 이미 적용됨"


def write_marker(site_packages: Path, results: dict[str, str], ok: bool) -> Path:
    """적용 결과를 기록한다. 실패한 실행도 기록하되 `ok: false`로 구분한다.

    `patch_set_version`만 보고 "적용됨"으로 판단하면 drift/conflict 상태의
    트리를 정상으로 오판한다. 마커를 소비하는 쪽은 반드시 `ok`를 함께 봐야 한다.
    """
    marker = site_packages / MARKER_FILENAME
    marker.write_text(
        json.dumps(
            {"patch_set_version": PATCH_SET_VERSION, "ok": ok, "results": results},
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return marker


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="NAT 벤더 라이브러리 패치 적용")
    parser.add_argument(
        "--venv",
        type=Path,
        required=True,
        help="패치 대상 venv 경로 (예: finus_nat/.venv)",
    )
    parser.add_argument(
        "--require",
        action="append",
        default=[],
        metavar="PATH",
        help=(
            "반드시 존재해야 하는 대상 (예: nat/plugins/mem0ai/memory.py). "
            "없으면 실패로 처리한다. 여러 번 지정할 수 있다."
        ),
    )
    args = parser.parse_args(argv)

    known = {patch.name for patch in PATCHES}
    unknown = sorted(set(args.require) - known)
    if unknown:
        print(
            f"[patch_vendor] --require에 정의되지 않은 대상이 있습니다: {unknown}. "
            f"정의된 대상: {sorted(known)}",
            file=sys.stderr,
        )
        return 2

    try:
        site_packages = find_site_packages(args.venv)
    except AmbiguousSitePackages as exc:
        print(f"[patch_vendor] {exc}", file=sys.stderr)
        return 1
    if site_packages is None:
        print(f"[patch_vendor] site-packages를 찾지 못했습니다: {args.venv}", file=sys.stderr)
        return 1

    print(f"[patch_vendor] site-packages: {site_packages}")

    results: dict[str, str] = {}
    failed = False
    for patch in PATCHES:
        try:
            status, message = apply_patch(site_packages, patch)
        except OSError as exc:
            # 파일별 쓰기(_write_patched)는 원자적이지만 이 반복 자체는 아니다:
            # patch #1이 성공하고 patch #3의 쓰기에서 OSError(디스크 가득 참, 권한
            # 문제 등)가 나면, 예외를 그냥 전파시킬 경우 write_marker가 전혀 호출되지
            # 않아 트리는 절반만 패치된 채로 남고 호출자는 마커도 없이 raw
            # traceback만 보게 된다. 여기서 잡아 이 대상만 실패로 기록하고 나머지
            # 대상은 계속 시도한다 - 한 파일의 쓰기 오류가 다른 대상의 진단 정보까지
            # 가려서는 안 된다.
            results[patch.name] = ERROR
            failed = True
            print(f"[patch_vendor] {ERROR:<8} {patch.name}: 쓰기 중 오류: {exc}", file=sys.stderr)
            continue
        results[patch.name] = status
        stream = sys.stderr if status in (DRIFT, CONFLICT, AMBIGUOUS, MISSING) else sys.stdout
        print(f"[patch_vendor] {status:<8} {message}", file=stream)
        if status in (DRIFT, CONFLICT, AMBIGUOUS):
            failed = True
        elif status == MISSING and patch.name in args.require:
            failed = True

    marker = write_marker(site_packages, results, ok=not failed)
    print(f"[patch_vendor] 마커 기록: {marker} (patch_set_version={PATCH_SET_VERSION}, ok={not failed})")

    if failed:
        print(
            "[patch_vendor] 패치를 적용하지 못했습니다. NAT/mem0 버전을 올렸다면 "
            "finus_nat/scripts/patch_vendor.py의 원본 문자열을 갱신하세요.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
