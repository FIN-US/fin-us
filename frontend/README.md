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
├── build-stamp.txt  # 위 번들이 어느 Assets/에서 나왔는지 적어 둔 트리 해시 (CI가 대조)
├── nginx.conf       # docker-compose의 frontend 서비스가 Build/를 서빙할 때 쓰는 nginx 설정
└── frontend.slnx    # Visual Studio 솔루션
```

## Build/를 git으로 추적하는 이유와 재빌드 규칙

### 왜 추적하는가

`frontend/Build/`는 **Unity 미설치 팀원이 프론트엔드를 로컬에서 바로 실행할 수 있도록** git으로 추적합니다.
Unity 에디터 없이도 아래 한 줄이면 대시보드가 뜹니다.

```bash
docker compose up frontend
# → http://localhost:8080 접속
```

`docker compose up frontend`를 쓰는 이유는 nginx가 정적 서빙과 `/api` 프록시를 함께 하기
때문입니다. 번들은 백엔드 주소를 모른 채 상대 경로로만 호출하므로(#246), 프록시 없이
정적 서버로만 띄우면 **화면은 뜨지만 백엔드 연동은 되지 않습니다.**

```bash
# 화면·레이아웃만 확인할 때. /api 프록시가 없어 API 호출은 404가 나고,
# 대시보드에는 실데이터 연결 실패 배너와 샘플 데이터가 표시됩니다.
python -m http.server 8080 --directory frontend/Build
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
4. 빌드가 끝나면 **곧바로** 스탬프를 갱신합니다.

   ```bash
   scripts/frontend_build_stamp.sh write
   ```

   이 스크립트는 지금 `frontend/Assets/`와 `frontend/Build/`의 git 트리 해시를
   `frontend/build-stamp.txt`에 적습니다. **"이 번들은 이 소스에서 나왔다"는 기록**이고,
   CI가 두 값을 모두 대조합니다. 그래서 **재빌드 직후에만** 실행해야 합니다 — 재빌드 없이
   실행하면 어긋난 상태에 도장을 찍는 것이라 검사 자체가 무의미해집니다.

   지난 스탬프 이후 `frontend/Build/`가 전혀 바뀌지 않았으면 스크립트가 거부합니다.
   소스를 고쳤는데 번들 바이트가 정말 동일하게 나온 경우에만 `write --force`로 넘기세요.
5. `git add frontend/Build/ frontend/build-stamp.txt && git commit`으로 소스 변경과 함께
   커밋합니다.

> `.gitignore`는 위 3개 경로만 화이트리스트합니다. 빌드 설정을 바꿔 다른 산출물
> (예: `Build/StreamingAssets/`)이 생기면 `git add`가 **에러 없이 건너뜁니다.**
> `git status --ignored frontend/Build/`로 누락을 확인하고 `.gitignore`를 함께 갱신하세요.

#### CI가 잡아 주는 것과 사람이 해야 하는 것

CI의 `unity-build-drift` 잡이 두 가지를 봅니다 (`.github/workflows/ci.yml`).

| 검사 | 무엇을 잡는가 |
| --- | --- |
| Assets 변경 시 Build 동반 커밋 여부 | 재빌드를 아예 빠뜨린 PR |
| 번들 스탬프와 현재 트리 대조 | 스탬프를 찍은 시점의 `Assets/`·`Build/` 조합과 지금 커밋된 것이 다른 경우 |

**자동으로 검증되는 것** — 커밋된 `frontend/Assets/`와 `frontend/Build/`가 마지막으로
스탬프를 찍은 시점의 그 조합인지. 재빌드를 잊었든, 재빌드 후 소스를 한 번 더 고치고
스탬프를 그대로 뒀든(#345의 원인), 번들을 손으로 고쳤든 PR이 빨간불이 됩니다.

**사람이 해야 하는 것 — `write`를 진짜 재빌드 직후에 실행하기.** 스탬프는 트리 해시일 뿐
"이 번들이 이 소스를 빌드해서 나왔다"를 증명하지 못합니다. 스크립트가 "지난 스탬프 이후
`Build/`가 안 바뀌었으면 거부"로 가장 흔한 우회를 막지만, **이건 사람의 규율을 돕는
과속방지턱이지 보증이 아닙니다.** 다음 두 경우는 CI를 그대로 통과합니다.

- `write --force`로 거부를 넘긴 경우.
- **커밋을 나눈 경우** — 커밋1에서 정상 재빌드하고, 커밋2에서 소스만 고친 뒤 다시 스탬프를
  찍으면 `Build/`가 커밋1에서 바뀌었으므로 첫 번째 검사가 통과하고, 스탬프도 최신 소스와
  맞으므로 두 번째 검사도 통과합니다. 이 저장소는 소스 수정과 `build:` 재빌드를 별도
  커밋으로 나누는 것이 관례라(예: `7bd1900` → `9c6f518`), 커밋 단위로 검사를 강제하면
  정상적인 PR이 전부 막힙니다. 그래서 그 방향은 택하지 않았습니다.

> 이 마지막 구멍을 진짜로 닫으려면 CI에서 Unity WebGL 빌드를 돌려 커밋된 번들과 대조하는
> 수밖에 없습니다(#345의 선택지 B). Unity 라이선스와 빌드 시간 비용 때문에 트리 해시
> 스탬프(선택지 A)를 택했고, 그 대가로 위 잔여 위험이 남습니다.

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
조건부 중계합니다. **nginx 중계가 정상이어도 backend가 403을 줄 수 있습니다** — backend는
핸드셰이크의 `Origin`을 `ALLOW_ORIGINS`와 대조하고 불일치 시 연결을 거부합니다(#256, 아래
참고). nginx는 `Origin`을 그대로 넘기므로 브라우저가 보낸 페이지 오리진이 그대로 검사
대상이 됩니다. `/api/v1/analyze`는 LLM 호출로 오래 걸려 `proxy_read_timeout`을 300s로
올려 뒀습니다.

> **`CORSMiddleware`는 아직 남겨 둡니다 — 단 `ALLOW_ORIGINS`는 그 뒤에도 계속 필요합니다.**
> `ApiClient`가 베이스 URL 없이 상대 경로(`/api/v1/...`)로 요청하고 번들도 재빌드됐으므로
> (#262), 브라우저는 항상 대시보드와 같은 오리진을 부르고 **HTTP 쪽** CORS 부담은
> 사라졌습니다.
> `CORSMiddleware`는 이제 걷어낼 수 있지만, 제거는 **후속 PR**로 분리했습니다 — 번들 교체와
> 서버 설정 축소를 한 커밋에 묶으면 문제가 생겼을 때 어느 쪽이 원인인지 가려내기 어렵습니다.
>
> **`ALLOW_ORIGINS`는 #246 이후에도 지우면 안 됩니다.** WebSocket(`/api/v1/ws`) 핸드셰이크의
> Origin 허용목록을 겸하기 때문입니다(#256). `CORSMiddleware`는 WebSocket 핸드셰이크에
> 적용되지 않아, 이 검사가 없으면 임의 사이트가 브로드캐스트를 수신할 수 있습니다
> (Cross-Site WebSocket Hijacking). 따라서 `http://localhost:8080`은 계속 포함돼야 하고
> (`backend/config.py`의 `ALLOW_ORIGINS` 기본값, `.env.example` 참고), 다른 호스트(예:
> Tailscale 주소)로 시연할 때는 해당 오리진을 `ALLOW_ORIGINS`에 추가해야 합니다. **#246
> 작업 시 이 변수를 정리 대상으로 삼지 마세요** — 지우면 화면은 정상적으로 뜨는데 실시간
> 알림 연결만 403으로 죽어서 원인을 찾기 어렵습니다.
>
> 베이스 URL을 상대 경로로 두면 포트를 고정하는 오리진 해석(`{Scheme}://{Host}:8000`)과 달리
> 443 뒤에서도 깨지지 않습니다. 다만 선행 슬래시가 붙은 root-relative 경로라 **서브패스까지
> 따라가지는 않습니다** — 대시보드를 `https://example.com/finus/`에 마운트하면 요청은
> `/finus/api/...`가 아니라 `/api/...`로 나가므로, 그 구성에서는 리버스 프록시가 `/api`를
> 루트에서 함께 중계해야 합니다. 그리고 **에디터 플레이 모드에는 페이지 오리진이 없어**
> (`Application.absoluteURL`이 빈 문자열) 상대 경로를 절대 URL로 만들 수 없습니다. 그래서
> `ApiClient.DefaultBaseUrl`은 에디터에서만 `http://localhost:8000`으로 폴백합니다 —
> 에디터로 테스트하려면 backend를 호스트에서 8000번으로 띄워 두세요.
>
> `backend`의 8000 포트는 **아직 전 인터페이스에 게시돼 있습니다**(`docker-compose.yml`).
> 재빌드 전에는 원격 시연이 8000 직접 호출에 기대고 있어 좁힐 수 없었지만, 이제 그 의존은
> 없어졌습니다. 좁히는 것도 위 `CORSMiddleware` 제거와 같은 후속 PR입니다. 좁힌 뒤에도 브라우저는 8080 프록시로, nginx는
> compose 내부 네트워크로 backend에 닿으므로 기능 영향은 없습니다.

현재 번들은 비압축입니다(`Build/`에 `.br`·`.gz` 산출물 없음).

압축은 텍스트 자산(`.css`, `.js`)에만 겁니다. **`.wasm`과 `.data`는 일부러 제외했습니다** —
nginx가 동적 gzip을 걸면 `Content-Length`를 지우고 chunked로 보내는데, `Build.loader.js`가
그 헤더로 수신 버퍼를 잡기 때문에 로딩바가 멈춘 것처럼 보입니다(PR #243 리뷰 2번).
대신 `gzip_static on`을 켜 두었으므로, Unity 빌드 설정을 압축 산출물로 바꿔 `.gz`를 함께
커밋하면 `Content-Length`를 유지한 채 그대로 서빙됩니다. `.br`까지 쓰려면 `Content-Encoding`
매핑을 별도로 추가해야 합니다.
