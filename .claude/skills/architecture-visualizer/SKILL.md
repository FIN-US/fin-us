---
name: architecture-visualizer
description: >
  프로젝트 코드베이스를 분석하여 1) 구조에 대한 요약 설명과 
  2) Mermaid.js 기반의 다이어그램이 포함된 종합 문서를 생성합니다.
  팀원 온보딩 및 코드 흐름 파악을 위해 "구조 요약해줘" 등의 요청 시 사용합니다.
---

# Architecture Visualizer Skill

산출물의 독자는 이 레포를 처음 보는 팀원이다. 폴더 목록이 아니라 **모듈의 책임과 호출 흐름**을
전달한다.

## 워크플로

### 1. 사전 준비 (Scan)

프로젝트 루트를 탐색한다. 다음은 무시한다.

- **점으로 시작하는 디렉토리 전부** (`.git`, `.claude`, `.github`, `.venv`, `.pytest_cache` …)
- 이름으로 지정한 제외 대상: `__pycache__`, `venv`, `node_modules`, `legacy`, `dist`, `build`

점 디렉토리를 이름으로 하나씩 나열하지 않는다. 그렇게 하면 `venv`는 막고 `.venv`는 놓치는
식으로 빠지는 것이 생긴다.

### 2. 구조 분석 (Analyze)

- 단순 폴더 나열을 하지 않는다. **각 모듈의 책임과 데이터/호출 흐름**을 뽑는다.
- `visualizer.py`는 확장자로 거르지 않는다. 파일 이름과 경로에 나타나는 신호
  (`fastapi`, `projectsettings`, `index.js` 등)로 모듈 성격을 판정한다.
- 아래 관계를 우선 매핑한다.
  - UI(Unity WebGL `frontend`) → API(`backend`) 호출
  - API(`backend`) → MCP 서버(`mcp-news`, `mcp-trading`, `mcp-dart`) 호출
  - NAT(`finus_nat`) ↔ MCP 서버 연결 및 라우팅
  - `scripts`가 어떤 실행 경로를 묶는지

### 3. 생성

```bash
python .claude/skills/architecture-visualizer/visualizer.py
```

`architecture.md`가 레포 루트에 생성된다. 스크립트가 실패하면 사유를 사용자에게 알린다.

### 4. 결과물

`architecture.md`는 아래 네 가지를 포함한다.

1. **핵심 아키텍처 한 줄 요약** (전체 흐름)
2. **핵심 모듈 역할** (각 폴더가 왜 존재하는지)
3. **모듈 간 상호작용** (A → B, 어떤 데이터/호출인지)
4. **Mermaid 다이어그램** (관계 중심)

저장 후 파일 위치를 사용자에게 알린다.

## 행동 원칙

- 다이어그램은 파일 트리가 아니라 **주요 모듈 간 관계**를 그린다. 노드가 많아지면 관계가 안 보인다.
- 항상 실행 시점의 파일 시스템을 기준으로 생성한다. 이전 `architecture.md`를 참고해 쓰지 않는다.
- Mermaid 문법 오류가 없는지 확인한다.
- **"해당 디렉토리는 주요 로직 및 관련 설정을 포함합니다" 같은 템플릿 문장을 쓰지 않는다.**
  모듈마다 실제로 다른 내용이 없으면 그 모듈은 적을 가치가 없다.
