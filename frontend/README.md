# frontend — Unity WebGL 프로젝트

이 디렉터리는 fin-us 서비스의 Unity WebGL 프론트엔드입니다.

## 디렉터리 구조 (주요 항목만)

```
frontend/
├── Assets/          # Unity 프로젝트 소스 (git 추적)
│   ├── Scripts/     # C# 스크립트 (ApiClient, PanelController 등)
│   ├── Scenes/      # Unity 씬 파일
│   ├── Graphics/    # 머티리얼, 셰이더
│   ├── Plugins/     # 네이티브 플러그인 (WebGL/FinUsWebInput.jslib)
│   ├── Resources/   # 런타임 리소스
│   └── WebGLTemplates/  # WebGL 템플릿 — Build/index.html·TemplateData/의 원본
│                        # (Build/ 쪽을 고치면 재빌드 시 덮어써집니다)
├── Packages/        # Unity 패키지 매니페스트 (git 추적)
├── ProjectSettings/ # Unity 프로젝트 설정 (git 추적)
├── Build/           # WebGL 빌드 산출물 — 아래 3개 경로만 선별 추적
│                    #   index.html / Build/** / TemplateData/**
├── nginx.conf       # docker-compose의 frontend 서비스가 Build/를 서빙할 때 쓰는 nginx 설정
└── frontend.slnx    # Visual Studio 솔루션
```

## Build/를 git으로 추적하는 이유와 재빌드 규칙

### 왜 추적하는가

`frontend/Build/`는 **Unity 미설치 팀원이 프론트엔드를 로컬에서 바로 실행할 수 있도록** git으로 추적합니다.
Unity 에디터 없이도 `frontend/Build/`를 **로컬 HTTP 서버로 서빙**하면 바로 확인할 수 있습니다.

```bash
python -m http.server 8080 --directory frontend/Build
# → http://localhost:8080 접속
```

> ⚠️ `frontend/Build/index.html`을 브라우저에서 **직접 열면(`file://`) 동작하지 않습니다.**
> Unity 로더가 `Build.data`·`Build.wasm`을 fetch로 가져오는데 `file://`에서는 CORS로 차단되고,
> `.wasm`도 `application/wasm` MIME 타입을 받지 못합니다. 반드시 HTTP로 서빙하세요.

> 원 결정 — `frontend/.gitignore` 주석 (커밋 `0cf37f4`, 2026-05-03에 추가):
> *"Keep the generated Unity WebGL build in git so teammates can run the frontend without installing Unity."*

### 재빌드 규칙 — 반드시 지켜야 합니다

**`frontend/Assets/`의 소스를 수정한 경우 — C# 스크립트(`Scripts/`)든 WebGL 템플릿
(`WebGLTemplates/`)이든 — Unity 에디터에서 WebGL 재빌드 후 `frontend/Build/`를 함께
커밋해야 합니다.**
소스만 커밋하고 빌드를 갱신하지 않으면, 추적 중인 번들과 소스가 어긋납니다.

재빌드 절차:

1. Unity Hub에서 이 프로젝트(`frontend/`)를 엽니다.
2. **File > Build Settings** 에서 플랫폼을 **WebGL**로 선택합니다.
3. **Build** 버튼을 누르고, 출력 폴더로 **`frontend/Build`를 선택**합니다.
   Unity가 선택한 폴더 *아래에* `Build/`·`TemplateData/`·`index.html`을 만들기 때문에
   최종 경로는 `frontend/Build/Build/Build.wasm` 형태가 됩니다 (이중 `Build/`가 정상입니다).
   `frontend`를 고르면 `.gitignore` 화이트리스트에 걸리지 않습니다.
4. `git add frontend/Build/ && git commit`으로 소스 변경과 함께 커밋합니다.

> `.gitignore`는 위 3개 경로만 화이트리스트합니다. 빌드 설정을 바꿔 다른 산출물
> (예: `Build/StreamingAssets/`)이 생기면 `git add`가 **에러 없이 건너뜁니다.**
> `git status --ignored frontend/Build/`로 누락을 확인하고 `.gitignore`를 함께 갱신하세요.

### 현재 상태 경고 (⚠️ 재빌드 후 이 섹션을 삭제하세요)

번들이 `a7e4a1c`(2026-05-30) 이후 갱신되지 않아, **PR #204에서 추가된 아래 계약이 번들에 반영되지 않은 상태**입니다:

- `price_known`
- `return_rate_known`
- `total_asset_is_estimate`

`PanelController`의 해당 분기는 소스 수준에서는 올바르게 구현됐으나, 현재 번들에서는 실행되지 않습니다.

**이 경고는 다음 WebGL 재빌드·커밋으로 해소됩니다. 해소한 커밋에서 이 섹션을 함께 삭제하세요.**

### 배포 파이프라인 현황

`docker-compose.yml`의 `frontend` 서비스가 `frontend/Build/`를 nginx로 정적 서빙합니다 (이슈 #236).
과거에는 이 서비스가 별도의 React 백엔드 연동 테스트 대시보드를 띄웠고 Unity 번들을 서빙하는
경로가 아예 없었습니다 (이슈 #212 조사 결과). 그 React 앱은 #236에서 제거됐습니다.

```bash
docker compose up frontend
# → http://localhost:8080 접속
```

| 항목 | 값 |
| --- | --- |
| 이미지 | `nginx:alpine` |
| 포트 | 호스트 `8080` → 컨테이너 `80` |
| 문서 루트 | `./frontend/Build` → `/usr/share/nginx/html` (읽기 전용 `:ro`) |
| nginx 설정 | `./frontend/nginx.conf` → `/etc/nginx/conf.d/default.conf` (읽기 전용) |
| `.wasm` MIME | `application/wasm` (`nginx.conf`에서 명시) |
| API 프록시 | `/api/`·`/health` → `http://backend:8000` (이슈 #245) |
| backend 의존 | `depends_on` 없음 — backend가 아직 없어도 nginx는 뜨고 정적 화면이 먼저 보입니다 |

### `/api` 리버스 프록시 (#245)

`nginx.conf`가 `/api/`와 `/health`를 같은 compose 네트워크의 `backend:8000`으로
프록시합니다. 브라우저 입장에서는 대시보드와 API가 **같은 오리진(8080)**이므로 CORS가
개입하지 않고, 어떤 주소로 열든(로컬·Tailscale·리버스 프록시 뒤) 그대로 동작합니다.

`proxy_pass`에는 상수 대신 변수(`set $backend_upstream backend:8000;`)를 씁니다. 상수
호스트명이면 nginx가 **기동 시점에** 이름을 해석하고 실패 시 아예 뜨지 않아 `depends_on`이
필요해지는데, 변수를 쓰면 Docker 내장 DNS(`resolver 127.0.0.11`)로 **요청 시점에** 해석하므로
backend를 기다리지 않아도 됩니다. backend가 없는 동안에는 `/api/` 호출만 502(이름 해석 실패·
연결 거부) 또는 504(`proxy_connect_timeout 5s` 만료, 재시작 중 stale IP로 향한 경우)가 납니다.
슬래시 없는 `/api`는 `location /api/` 프리픽스에 걸리지 않아 정적 라우트의 404입니다.

`frontend` 서비스에 healthcheck를 추가할 일이 생기면 `/health`가 아니라 **`/nginx-health`**를
쓰세요. `/health`는 backend로 프록시되므로, backend가 죽으면 frontend까지 unhealthy가 되어
`depends_on`을 뺀 위 설계가 그대로 무효가 됩니다.

`/api/v1/ws`(WebSocket)는 `map $http_upgrade $connection_upgrade`로 업그레이드 헤더를
조건부 중계합니다. `/api/v1/analyze`는 LLM 호출로 오래 걸려 `proxy_read_timeout`을 300s로
올려 뒀습니다.

> **CORS 설정은 아직 남겨 둡니다.** 현재 Unity 번들은 `http://localhost:8000`을 하드코딩해
> 여전히 8000번을 직접 호출합니다. 번들이 상대 경로를 쓰도록 바뀌는 시점(이슈 #246,
> WebGL 재빌드 필요)부터 프록시 경로만 타게 되며, 그때 `ALLOW_ORIGINS` 수동 관리가
> 실제로 사라집니다. 그전까지는 backend의 CORS 허용 오리진에 `http://localhost:8080`이
> 포함돼야 하고(`backend/config.py`의 `ALLOW_ORIGINS` 기본값, `.env.example` 참고),
> 다른 호스트(예: Tailscale 주소)로 시연할 때는 해당 오리진을 `ALLOW_ORIGINS`에 추가하세요.

현재 번들은 비압축입니다(`Build/`에 `.br`·`.gz` 산출물 없음).

압축은 텍스트 자산(`.css`, `.js`)에만 겁니다. **`.wasm`과 `.data`는 일부러 제외했습니다** —
nginx가 동적 gzip을 걸면 `Content-Length`를 지우고 chunked로 보내는데, `Build.loader.js`가
그 헤더로 수신 버퍼를 잡기 때문에 로딩바가 멈춘 것처럼 보입니다(PR #243 리뷰 2번).
대신 `gzip_static on`을 켜 두었으므로, Unity 빌드 설정을 압축 산출물로 바꿔 `.gz`를 함께
커밋하면 `Content-Length`를 유지한 채 그대로 서빙됩니다. `.br`까지 쓰려면 `Content-Encoding`
매핑을 별도로 추가해야 합니다.
