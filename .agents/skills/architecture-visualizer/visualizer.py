import os
from pathlib import Path

EXCLUDE = {
    ".git",
    ".agents",
    "__pycache__",
    "venv",
    "node_modules",
    ".idea",
    ".vscode",
    ".DS_Store",
    ".claude",
    ".gemini",
    ".github",
    ".cursor",
    "legacy",
}
SOURCE_EXTS = (".py", ".java", ".ts", ".tsx", ".js", ".jsx", ".md", ".yml", ".yaml", ".json")


def _safe_read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except Exception:
        return ""


def _top_level_dirs(start_path: str) -> list[Path]:
    root = Path(start_path).resolve()
    dirs = []
    for child in root.iterdir():
        if child.is_dir() and child.name not in EXCLUDE:
            dirs.append(child)
    return sorted(dirs, key=lambda p: p.name)


def _collect_signals(dir_path: Path) -> dict[str, bool]:
    files = list(dir_path.rglob("*"))
    names = {f.name for f in files if f.is_file()}
    paths_text = "\n".join(str(f) for f in files if f.is_file())
    joined = f"{' '.join(names)}\n{paths_text}".lower()
    return {
        "fastapi": "fastapi" in joined or "uvicorn" in joined,
        "unity": "projectsettings" in joined or ".unity" in joined or "build.wasm" in joined,
        "mcp": "modelcontextprotocol" in joined or "call_tool" in joined or "index.js" in joined,
        "yaml_agents": "configs/agents" in joined or "router.yml" in joined,
        "ops_scripts": "docker-compose" in joined or "run_stack.sh" in joined or "setup_deps.sh" in joined,
    }


def _infer_role(dir_path: Path, signals: dict[str, bool]) -> str:
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
    if name == "scripts":
        return "로컬/도커 실행, 의존성 설치, 환경 점검 등 운영 자동화를 담당합니다."
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
    if "nat_finus_nat" in backend_text:
        rels.append(("backend", "finus_nat", "NAT 함수/워크플로 활용"))

    finus_text = _safe_read(finus_api)
    if "subdir=\"mcp-news\"" in finus_text:
        rels.append(("finus_nat", "mcp-news", "MCP 서버 래핑"))
    if "subdir=\"mcp-trading\"" in finus_text:
        rels.append(("finus_nat", "mcp-trading", "MCP 서버 래핑"))

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
        role = _infer_role(d, _collect_signals(d))
        lines.append(f"- **{d.name}/**: {role}")

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
    print(f"✅ 완료: {output_file} 파일에 설명과 도식이 업데이트되었습니다.")


if __name__ == "__main__":
    run_visualizer()