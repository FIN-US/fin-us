# backend pip to uv 전환 설계

## 배경

Issue #33은 `backend/` 서비스의 패키지 관리 방식을 pip/pip-compile에서 uv로 전환하는 작업이다. `finus_nat/`은 이미 `pyproject.toml`, `uv.lock`, `uv sync` 흐름을 사용하지만, `backend/`는 `requirements.txt`, `requirements-dev.txt`, `requirements.lock`과 `pip install` 흐름을 사용한다. 이 불일치를 줄이고 Docker 빌드의 Python 의존성 설치 단계를 단축하는 것이 목표다.

이번 작업은 issue #28의 서브이슈로 다룬다. 범위는 `backend/` 단독 전환이며, 루트 uv workspace로 `backend`와 `finus_nat`을 묶는 구조 변경은 제외한다.

## 목표

- `backend/`의 의존성 선언 원천을 `backend/pyproject.toml`로 변경
- `backend/uv.lock` 기반의 재현 가능한 설치 흐름 구성
- 런타임 의존성과 테스트/dev 의존성 분리
- `backend/Dockerfile`의 Python 설치 단계를 uv 기반으로 전환
- `docker-compose.yml`의 backend 가상환경 볼륨 경로를 uv 기본 경로인 `.venv`에 맞춤
- README의 backend 실행 명령을 uv 기준으로 갱신

## 비목표

- 루트 `pyproject.toml` 또는 uv workspace 도입
- `finus_nat/` 패키지 구조 변경
- backend 앱 코드, API 동작, 테스트 코드 변경
- MCP Node 서버의 패키지 관리 방식 변경
- pip 경로와 uv 경로의 장기 병행 지원

## 접근안 비교

### 선택안: backend 독립 uv project

`backend/pyproject.toml`과 `backend/uv.lock`을 추가하고, 기존 `requirements.txt`, `requirements-dev.txt`, `requirements.lock`을 제거한다. Dockerfile은 `finus_nat/Dockerfile`처럼 `ghcr.io/astral-sh/uv:0.9.28` 이미지에서 uv 바이너리를 복사한 뒤 `uv sync --no-dev --frozen`으로 런타임 의존성을 설치한다.

이 방식은 issue #33 범위와 가장 잘 맞고, `finus_nat`과 운영 패턴을 맞추면서도 repo 전체 구조 변경을 피한다.

### 대안: uv pip install만 사용

기존 requirements 파일을 유지하고 `uv pip install -r`만 사용하는 방식이다. 설치 속도는 개선될 수 있지만, 의존성 선언과 lock 관리가 pip 계열로 남아 패키지 관리 시스템 통일 목표에는 부족하다.

### 대안: 루트 uv workspace

루트에서 `backend`와 `finus_nat`을 하나의 uv workspace로 묶는 방식이다. 장기적으로 일관성은 높지만, 현재 실행 구조와 repo 레이아웃을 더 크게 바꾸므로 이번 서브이슈 범위를 넘는다.

## 설계

### pyproject 구성

`backend/pyproject.toml`은 패키지 빌드용 설정이 아니라 uv 의존성 관리용 project 파일로 둔다. 현재 backend는 루트에서 `uvicorn backend.main:app` 형태로 실행되는 모듈 구조이므로, 이번 전환에서 setuptools 설정이나 entry point는 추가하지 않는다.

`requires-python`은 Docker 베이스 이미지와 맞춰 `>=3.13,<3.14`로 둔다. `project.dependencies`에는 FastAPI 앱 런타임에 필요한 의존성만 넣고, `pytest`, `pytest-asyncio`는 dev dependency로 분리한다. 버전 범위는 기존 `backend/requirements.txt`의 선언을 그대로 옮기는 것을 기본으로 한다.

### Dockerfile 구성

`backend/Dockerfile`의 Node MCP 서버 설치 흐름은 유지한다. Python 의존성 설치 단계만 pip에서 uv로 바꾼다.

의존성 캐시 효율을 위해 `backend/pyproject.toml`과 `backend/uv.lock`을 먼저 복사하고 `uv sync --project ./backend --no-dev --frozen`을 실행한 뒤 전체 소스를 복사한다. 런타임 명령은 `finus_nat`과 같은 패턴으로 `uv run --project ./backend uvicorn backend.main:app ...`을 사용한다.

개발 편의를 위한 기존 `--reload` 동작은 유지한다.

### docker-compose 구성

backend 서비스의 익명 볼륨은 `/app/backend/venv`에서 `/app/backend/.venv`로 바꾼다. uv 기본 가상환경 경로와 맞추기 위한 변경이며, host checkout의 `.venv` 오염을 막는 기존 목적은 유지한다.

### 문서 구성

README의 backend 설치 및 실행 예시는 uv 명령으로 바꾼다.

예상 명령은 다음과 같다.

```bash
uv sync --project backend
uv run --project backend uvicorn backend.main:app --host 0.0.0.0 --port 8787
uv run --project backend pytest backend/tests
```

## 파일 변경 범위

- 삭제: `backend/requirements.txt`
- 삭제: `backend/requirements-dev.txt`
- 삭제: `backend/requirements.lock`
- 추가: `backend/pyproject.toml`
- 추가: `backend/uv.lock`
- 수정: `backend/Dockerfile`
- 수정: `docker-compose.yml`
- 수정: `README.md`

설계 문서 외의 위 파일 변경은 구현 단계에서 수행한다.

## 오류 처리

uv 전환은 앱 런타임 로직을 바꾸지 않는다. 설치 및 lockfile 문제는 빌드와 테스트 단계에서 명확히 실패하게 둔다.

- lockfile 불일치: `uv sync --frozen` 실패
- 누락 런타임 의존성: backend import 또는 API 테스트 실패
- 누락 dev 의존성: uv 기반 pytest 실행 실패
- compose volume 경로 불일치: backend 컨테이너 실행 검증 실패

pip fallback은 남기지 않는다. fallback을 유지하면 전환 완료 여부가 흐려지고, 두 패키지 관리 흐름을 동시에 관리해야 한다.

## 검증 기준

구현 완료 후 다음을 확인한다.

```bash
uv lock --project backend --check
uv run --project backend pytest backend/tests
docker compose build backend
docker compose config
git diff --check
```

전체 compose 기동은 `finus-nat`, Redis, 외부 API 환경 변수 상태에 영향을 받는다. 필요한 경우 `docker compose up -d backend`까지 확인하되, 실패 시 의존 서비스나 환경 변수 문제와 uv 전환 문제를 구분해서 판단한다.

## 결정 사항

- `backend/` 단독 uv project로 전환한다.
- 루트 uv workspace는 도입하지 않는다.
- `backend/pyproject.toml`은 의존성 관리용으로만 사용한다.
- `pytest`, `pytest-asyncio`는 dev dependency로 분리한다.
- Docker runtime 설치는 `uv sync --no-dev --frozen`을 사용한다.
- pip 기반 requirements 파일은 제거한다.
