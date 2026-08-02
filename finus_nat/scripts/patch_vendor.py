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
않았다.** 이 스크립트는 세 상태를 구분한다.

- `already`  — sentinel이 이미 있음. 재실행해도 안전(멱등).
- `applied`  — 기대한 원본을 찾아 치환함.
- `drift`    — 파일은 있는데 sentinel도 원본도 없음. **상류가 바뀐 것이므로 실패로 처리한다.**
- `conflict` — sentinel과 원본이 동시에 존재. sentinel 오탐이므로 실패로 처리한다.

## 종료 코드

- `0` — 모든 대상이 applied/already. `--require` 없는 missing도 여기 포함.
- `1` — drift 또는 conflict가 하나라도 있거나, `--require` 대상이 없거나,
        site-packages를 못 찾았거나 후보가 모호함.
- `2` — 인자 오류 (정의되지 않은 `--require` 이름 등).

`missing`(대상 파일 없음)은 기본적으로 경고만 남긴다. 로컬 venv는 optional extra가
빠져 있을 수 있기 때문이다. 반드시 있어야 하는 대상만 `--require`로 지정해 실패로
승격시킨다. Dockerfile이 종전에 유일하게 존재를 강제하던 `memory.py`가 그 대상이다.
(`--require`를 전부에 걸면 상류가 파일을 재배치했을 때 빌드가 통째로 깨지므로,
보장 범위를 종전과 동일하게 유지한다.)

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
import sys
from dataclasses import dataclass, field
from pathlib import Path

# 패치 내용이 바뀔 때마다 올린다. 마커 파일과 비교해 재적용 필요 여부를 판단한다.
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
        if replacement.old not in text:
            # 상류가 바뀌었다. 조용히 넘어가면 런타임에 원인 불명으로 실패한다.
            return DRIFT, (
                f"{patch.name} 치환 #{index}: 기대한 원본을 찾지 못했고 적용 표식도 없습니다. "
                f"NAT/mem0 상류가 바뀐 것으로 보입니다. sentinel={replacement.sentinel!r}"
            )
        text = text.replace(replacement.old, replacement.new, 1)
        applied.append(str(index))

    # run.sh는 마지막 치환 블록 안에서만 write_text를 호출해, 앞선 치환이 조용히
    # 버려질 수 있었다. 여기서는 루프 밖에서 한 번만 쓴다.
    if text != original:
        target.write_text(text, encoding="utf-8")
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
        status, message = apply_patch(site_packages, patch)
        results[patch.name] = status
        stream = sys.stderr if status in (DRIFT, CONFLICT, MISSING) else sys.stdout
        print(f"[patch_vendor] {status:<8} {message}", file=stream)
        if status in (DRIFT, CONFLICT):
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
