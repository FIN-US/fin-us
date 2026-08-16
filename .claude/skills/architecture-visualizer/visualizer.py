import os
import sys
from pathlib import Path

# 점으로 시작하는 디렉토리는 이름을 나열하지 않고 일괄 제외한다(_is_excluded 참조).
# 예전에는 이 집합에 하나씩 적었는데, `venv`는 있고 `.venv`는 없어서 루트의 `.venv`,
# `.venv-1`, `.pytest_cache`, `.codex-worktrees` 등이 전부 "핵심 모듈"로 출력됐다.
EXCLUDE = {
    "__pycache__",
    "venv",
    "node_modules",
    "legacy",
    "dist",
    "build",
}
# _collect_signals가 훑는 파일 수 상한. 신호 판정은 몇 개만 봐도 충분한데, 상한이 없으면
# Unity 프로젝트(frontend)나 실수로 걸린 .venv를 통째로 rglob해 수 초씩 멈춘다.
SIGNAL_FILE_LIMIT = 2000


def _is_excluded(name: str) -> bool:
    return name.startswith(".") or name in EXCLUDE


def _safe_read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except Exception:
        return ""


def _top_level_dirs(start_path: str) -> list[Path]:
    root = Path(start_path).resolve()
    dirs = []
    for child in root.iterdir():
        if child.is_dir() and not _is_excluded(child.name):
            dirs.append(child)
    return sorted(dirs, key=lambda p: p.name)


def _collect_signals(dir_path: Path) -> dict[str, bool]:
    # os.walk로 훑으면서 제외 대상 디렉토리는 내려가기 전에 쳐낸다. rglob은 가지치기가
    # 안 돼서 node_modules/.venv 안까지 다 들어간다.
    paths: list[str] = []
    for current, subdirs, filenames in os.walk(dir_path):
        subdirs[:] = [d for d in subdirs if not _is_excluded(d)]
        for filename in filenames:
            paths.append(f"{filename}\n{os.path.join(current, filename)}")
            if len(paths) >= SIGNAL_FILE_LIMIT:
                break
        if len(paths) >= SIGNAL_FILE_LIMIT:
            break
    joined = "\n".join(paths).lower()
    return {
        "fastapi": "fastapi" in joined or "uvicorn" in joined,
        "unity": "projectsettings" in joined or ".unity" in joined or "build.wasm" in joined,
        "mcp": "modelcontextprotocol" in joined or "call_tool" in joined or "index.js" in joined,
        "yaml_agents": "configs/agents" in joined or "router.yml" in joined,
        "ops_scripts": "docker-compose" in joined or "run_stack.sh" in joined or "setup_deps.sh" in joined,
    }


def _infer_role(dir_path: Path) -> str:
    # 이름으로 판정되는 디렉토리가 대부분이라 _collect_signals는 필요할 때만 부른다.
    # 항상 먼저 계산하면 아는 디렉토리까지 트리를 훑는다.
    name = dir_path.name
    if name == "backend":
        return "FastAPI 오케스트레이션 계층으로, MCP/LLM 호출을 조합해 분석 API를 제공합니다."
    if name == "frontend":
        return "Unity WebGL UI 계층으로, backend API를 호출해 포트폴리오와 분석 결과를 시각화합니다. 빌드 산출물(Build/)은 compose의 nginx가 정적 서빙합니다."
    if name == "finus_nat":
        return "NAT 워크플로 레이어로, 라우터/브랜치 에이전트와 MCP tool 래퍼를 통해 멀티 에이전트 실행을 담당합니다."
    if name == "mcp-news":
        return "뉴스/수급/리서치 데이터를 제공하는 MCP 서버입니다."
    if name == "mcp-trading":
        return "잔고 조회 및 주문 실행 등 트레이딩 기능을 제공하는 MCP 서버입니다."
    if name == "mcp-dart":
        return "OpenDART 공시 신호를 제공하는 MCP 서버입니다."
    if name == "scripts":
        return "로컬/도커 실행, 의존성 설치, 환경 점검 등 운영 자동화를 담당합니다."
    if name == "docs":
        return "설계·조사 문서를 모아 둡니다. 실행 코드는 없습니다."
    signals = _collect_signals(dir_path)
    if signals["fastapi"]:
        return "API 서버 역할의 백엔드 모듈입니다."
    if signals["unity"]:
        return "Unity 기반 프런트엔드 모듈입니다."
    if signals["mcp"]:
        return "외부 도구/데이터를 표준 인터페이스로 제공하는 MCP 모듈입니다."
    if signals["yaml_agents"]:
        return "에이전트 라우팅/전략 설정을 관리하는 구성 모듈입니다."
    if signals["ops_scripts"]:
        return "실행/배포 자동화를 위한 운영 모듈입니다."
    return "도메인 또는 지원 기능을 담는 모듈입니다."


def _detect_relationships(start_path: str) -> list[tuple[str, str, str]]:
    root = Path(start_path).resolve()
    rels: list[tuple[str, str, str]] = []

    frontend_api_client = root / "frontend" / "Assets" / "Scripts" / "ApiClient.cs"
    backend_main = root / "backend" / "main.py"
    finus_api = root / "finus_nat" / "src" / "nat_finus_nat" / "finus_api.py"
    scripts_dir = root / "scripts"

    api_client_text = _safe_read(frontend_api_client)
    if "/api/v1/analyze" in api_client_text or "/api/v1/news" in api_client_text:
        rels.append(("frontend", "backend", "HTTP API 호출"))

    backend_text = _safe_read(backend_main)
    if "mcp-news" in backend_text or "NEWS_MCP_PARAMS" in backend_text:
        rels.append(("backend", "mcp-news", "MCP tool 호출"))
    if "mcp-trading" in backend_text or "TRADING_MCP_PARAMS" in backend_text:
        rels.append(("backend", "mcp-trading", "MCP tool 호출"))
    if "mcp-dart" in backend_text or "DART_MCP_PARAMS" in backend_text:
        rels.append(("backend", "mcp-dart", "MCP tool 호출"))
    if "nat_finus_nat" in backend_text:
        rels.append(("backend", "finus_nat", "NAT 함수/워크플로 활용"))

    finus_text = _safe_read(finus_api)
    if "subdir=\"mcp-news\"" in finus_text:
        rels.append(("finus_nat", "mcp-news", "MCP 서버 래핑"))
    if "subdir=\"mcp-trading\"" in finus_text:
        rels.append(("finus_nat", "mcp-trading", "MCP 서버 래핑"))
    if "subdir=\"mcp-dart\"" in finus_text:
        rels.append(("finus_nat", "mcp-dart", "MCP 서버 래핑"))

    if scripts_dir.is_dir():
        rels.append(("scripts", "backend", "실행/배포 스크립트"))
        rels.append(("scripts", "frontend", "실행/배포 스크립트"))
        rels.append(("scripts", "finus_nat", "실행/설치 스크립트"))

    # dedupe while preserving order
    seen = set()
    unique_rels = []
    for rel in rels:
        if rel not in seen:
            seen.add(rel)
            unique_rels.append(rel)
    return unique_rels


def get_architecture_summary(start_path: str = ".") -> str:
    dirs = _top_level_dirs(start_path)
    rels = _detect_relationships(start_path)

    lines = []
    lines.append("### 📝 아키텍처 요약")
    lines.append("이 프로젝트는 **UI → Orchestrator API → MCP 데이터 공급자** 흐름과, 별도의 **NAT 멀티 에이전트 워크플로**를 함께 운용합니다.\n")
    lines.append("#### 핵심 모듈 역할")
    for d in dirs:
        lines.append(f"- **{d.name}/**: {_infer_role(d)}")

    if rels:
        lines.append("\n#### 모듈 간 상호작용")
        for src, dst, label in rels:
            lines.append(f"- **{src} → {dst}**: {label}")
    return "\n".join(lines)


def generate_mermaid_code(start_path: str = ".") -> str:
    rels = _detect_relationships(start_path)
    lines = ["graph LR"]
    for src, dst, label in rels:
        s = src.replace("-", "_")
        d = dst.replace("-", "_")
        lines.append(f"    {s} -->|{label}| {d}")
    return "\n".join(lines)


def run_visualizer(output_file: str = "architecture.md"):
    summary = get_architecture_summary()
    diagram = generate_mermaid_code()
    with open(output_file, "w", encoding="utf-8") as f:
        f.write("# 프로젝트 구조 및 아키텍처\n\n")
        f.write(summary)
        f.write("\n\n### 📊 상호작용 다이어그램\n")
        f.write("```mermaid\n")
        f.write(diagram)
        f.write("\n```\n")
    print(f"완료: {output_file} 파일에 설명과 도식이 업데이트되었습니다.")


if __name__ == "__main__":
    # Windows 기본 콘솔은 cp949라 비ASCII 출력이 UnicodeEncodeError로 죽는다. 파일은 이미
    # utf-8로 정상 생성된 뒤라 실제로는 성공인데 exit 1이 되어 실패로 보고된다.
    # 인코딩을 못 바꾸는 스트림(리다이렉트 등)도 있으므로 실패해도 진행한다.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass
    run_visualizer()
