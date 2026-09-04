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
├── nginx.conf.template  # frontend 서비스의 nginx 설정 — envsubst로 API 키를 주입하는 템플릿
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
- **`Build/`가 재빌드 아닌 이유로 바뀐 상태에서 `write`한 경우** — 번들 파일을 손으로
  고쳤거나, main의 재빌드 커밋을 머지하면서 스탬프는 내 것을 남긴 뒤 소스만 고친 경우가
  그렇습니다. 가드는 "지난 스탬프 이후 `Build/`가 바뀌었는가"만 보므로, 그 변화가 **이
  소스를** 빌드한 결과인지는 구분하지 못합니다.

반대로, **"커밋1에서 재빌드하고 커밋2에서 소스만 고친 뒤 재스탬프"는 가드에 걸립니다** —
커밋2 시점에는 `Build/`가 지난 스탬프 이후 그대로라 `write`가 거부합니다. 그래도 커밋 단위
검사를 도입하지 않은 이유는 따로 있습니다. 이 저장소는 소스 수정과 `build:` 재빌드를 별도
커밋으로 나누는 것이 관례라(예: `7bd1900` → `9c6f518`), 커밋마다 스탬프 일치를 요구하면
정상적인 PR이 전부 막힙니다.

> 위 잔여 경로를 진짜로 닫으려면 CI에서 Unity WebGL 빌드를 돌려 커밋된 번들과 대조하는
> 수밖에 없습니다(#345의 선택지 B). Unity 라이선스와 빌드 시간 비용 때문에 트리 해시
> 스탬프(선택지 A)를 택했고, 그 대가로 이 위험이 남습니다.

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
| nginx 설정 | `./frontend/nginx.conf.template` → `/etc/nginx/templates/default.conf.template` (읽기 전용) — 기동 시 envsubst가 `/etc/nginx/conf.d/default.conf`를 만듭니다 |
| `.wasm` MIME | `application/wasm` (`nginx.conf.template`에서 명시) |
| API 프록시 | `/api/`·`/health` → `http://backend:8000` (이슈 #245) |
| 레이트리밋 | `/api/` 2r/s, `/api/v1/analyze` 6r/m + 동시 2건, 초과 시 429 (이슈 #266 1단계) |
| API 인증 | `FINUS_API_KEY`를 채운 배포에서만 (이슈 #266 2·3단계) — nginx가 키를 쿠키로 내려 주므로 **번들 재빌드 없이 동작합니다**, 아래 참고 |
| backend 의존 | `depends_on` 없음 — backend가 아직 없어도 nginx는 뜨고 정적 화면이 먼저 보입니다 |

### `/api` 리버스 프록시 (#245)

`nginx.conf.template`이 `/api/`와 `/health`를 같은 compose 네트워크의 `backend:8000`으로
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

> **`CORSMiddleware`는 제거했습니다 — 단 `ALLOW_ORIGINS`는 그 뒤에도 계속 필요합니다.**
> `ApiClient`가 베이스 URL 없이 상대 경로(`/api/v1/...`)로 요청하고 번들도 재빌드됐으므로
> (#262), 브라우저는 항상 대시보드와 같은 오리진을 부르고 **HTTP 쪽** CORS 부담은
> 사라졌습니다. 아무도 타지 않는 미들웨어를 남겨 두면 "동작 중인 보호"처럼 보이므로
> 걷어냈습니다(#246). 애초에 CORS는 응답을 *읽는* 것만 막고 요청 *실행*은 막지 못하므로,
> 무인증 API의 방어가 아니었습니다 — 그 자리는 아래 레이트리밋(#266 1단계)이 맡습니다.
>
> **`ALLOW_ORIGINS`는 지우면 안 됩니다.** WebSocket(`/api/v1/ws`) 핸드셰이크의
> Origin 허용목록을 겸하기 때문입니다(#256). `CORSMiddleware`는 WebSocket 핸드셰이크에
> 적용되지 않아, 이 검사가 없으면 임의 사이트가 브로드캐스트를 수신할 수 있습니다
> (Cross-Site WebSocket Hijacking). 미들웨어가 사라진 지금은 **이 검사가 그 목록의 유일한
> 소비자**라 더더욱 쓰이지 않는 설정으로 보이기 쉽습니다. `http://localhost:8080`은 계속
> 포함돼야 하고(`backend/config.py`의 `ALLOW_ORIGINS` 기본값, `.env.example` 참고), 다른
> 호스트(예: Tailscale 주소)로 시연할 때는 해당 오리진을 `ALLOW_ORIGINS`에 추가해야 합니다.
> 지우면 화면은 정상적으로 뜨는데 실시간 알림 연결만 403으로 죽어서 원인을 찾기 어렵습니다.
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
> `backend`의 8000 포트는 **`127.0.0.1`에만 게시됩니다**(`docker-compose.yml`, #246).
> 재빌드 전에는 원격 시연이 8000 직접 호출에 기대고 있어 좁힐 수 없었지만 그 의존이
> 없어졌고, 열어 두면 아래 레이트리밋을 8000 직접 호출로 그대로 우회할 수 있습니다.
> 브라우저는 8080 프록시로, nginx는 compose 내부 네트워크로 backend에 닿으므로 기능
> 영향은 없습니다. 호스트에서 `/docs`를 열거나 수동으로 API를 부르는 것, Unity 에디터
> 플레이 모드(`http://localhost:8000` 폴백)는 그대로 됩니다 — 다른 기기에서 8000으로
> 직접 붙던 흐름만 SSH 포트포워딩(`ssh -L 8000:127.0.0.1:8000 <호스트>`)이 필요합니다.

### 레이트리밋 (#266 1단계)

`/api/v1/analyze`는 **인증이 없으면서** 호출 한 번이 LLM(OpenAI/Anthropic) 또는 NAT 멀티
에이전트를 태워 직접 과금으로 이어집니다. CORS는 이 위험의 방어가 되지 않습니다 — 응답을
*읽는* 것만 막고 요청 *실행*은 막지 못하므로, 임의 페이지의
`fetch(..., { mode: "no-cors" })` 한 줄이면 호출이 실제로 나갑니다. 그래서 제한은 프록시
지점인 nginx에 걸었습니다. backend 코드를 건드리지 않고 한 자리에서 걸립니다.

| 경로 | 제한 | 근거 |
| --- | --- | --- |
| `/api/v1/analyze` | 6r/m (burst 2) + **동시 2건** | LLM·NAT 과금 경로 |
| 그 밖의 `/api/`·`/health` | 2r/s (burst 20) | MCP·KIS 외부 API 호출 |
| 정적 자산·`/nginx-health` | 없음 | backend를 거치지 않습니다 |

레이트만으로는 부족해 **동시 연결**도 함께 제한합니다. analyze 한 건은
`proxy_read_timeout 300s`가 허용하는 만큼 살아 있을 수 있어서, 간격을 지키며 천천히 밀어
넣어도 진행 중인 LLM 호출은 계속 쌓이기 때문입니다.

초과 응답은 **429**이고, 본문은 nginx 기본 HTML이 아니라 FastAPI와 같은 모양의
`{"detail": "..."}` JSON입니다. `ApiClient.ExtractErrorMessage`가 그 키를 읽어 배너에 그대로
싣기 때문에, **번들을 다시 굽지 않고도** 사용자에게 읽히는 안내가 나갑니다.

두 거절은 성질이 달라 본문을 갈라 뒀습니다. 레이트 초과는 잠깐 기다리면 풀리지만, 동시 실행
초과는 진행 중인 분석이 끝나야 풀리고 그건 `proxy_read_timeout 300s`까지 갈 수 있습니다.

| 거절 | 클라이언트가 보는 코드 | `Retry-After` | 배너 문구 |
| --- | --- | --- | --- |
| `limit_req` | 429 | `10` | 요청이 너무 잦습니다… |
| `limit_conn` | 429 | **없음** | 이미 진행 중인 분석이 있습니다… |

`limit_conn` 쪽에 `Retry-After`를 붙이지 않은 것은 의도입니다 — 언제 풀릴지는 진행 중인
분석의 남은 시간에 달렸는데 서버가 그걸 모릅니다. 레이트 쪽 값(10초)을 복사해 넣으면 그
시각에 다시 429이고, 상한인 300초를 적으면 대개 필요 이상으로 기다리게 합니다.

`error_page`는 상태 코드로만 갈라낼 수 있어서 `limit_conn_status`를 **내부적으로만** 430으로
둡니다. IANA 미할당 코드이고 `error_page`가 가로채므로 클라이언트에게는 나가지 않습니다
(실측 확인). 문구를 바꾸려면 `nginx.conf.template`의 `@too_many_requests`·`@analysis_in_flight`
블록만 고치면 됩니다.

**스케줄러의 자동 분석은 이 제한에 걸리지 않습니다.** `backend/scheduler.py`가
`perform_stock_analysis`를 같은 프로세스에서 직접 부르고 HTTP를 타지 않기 때문입니다.
감시 주기(10분)와 위 한도를 맞출 필요가 없는 것도 그래서입니다.

제한이 실제로 의미를 가지려면 **8000 직접 호출이 막혀 있어야 합니다.** 그래서 같은 작업에서
`backend`의 게시를 `127.0.0.1`로 좁혔습니다(#246). 둘 중 하나만 하면 "막는 게 아니라 막는
것처럼 보이는" 상태가 됩니다. 이 배선은 `backend/tests/test_nginx_rate_limit.py`와
`backend/tests/test_compose_ports.py`가 고정합니다 — CI의 `nginx -t`는 문법만 보므로
`limit_req` 한 줄이 사라져도 통과합니다.

### API 인증 (#266 2·3단계) — nginx가 키를 쿠키로 내려 줍니다

위 레이트리밋은 **비용의 뚜껑**이지 접근 제어가 아닙니다. 접근 제어는 backend의 정적 API 키가
맡습니다(`backend/main.py`). `.env`의 `FINUS_API_KEY`를 채우면 `/api/` 아래 모든 요청과
`/api/v1/ws` 핸드셰이크가 이 키를 요구합니다. 비워 두면(기본값) 인증이 꺼지고 backend 기동
로그에 경고가 남습니다.

키가 서버에 닿는 경로는 둘입니다.

| 클라이언트 | 전달 경로 | 누가 붙이나 |
| --- | --- | --- |
| 브라우저(이 번들) | `finus_api_key` 쿠키 | **브라우저** — 아래 nginx가 문서 응답에 실어 보냅니다 |
| 비브라우저(`curl`·`wscat`·NAT) | `X-API-Key` 헤더 | 호출자가 직접 |

그래서 **이 번들은 고치지 않아도 인증을 통과합니다.** `ApiClient`는 여전히 아무 자격증명도
붙이지 않지만, 쿠키는 브라우저가 same-origin 요청에 자동으로 붙이고 그건 WebSocket
핸드셰이크에도 적용됩니다. 위의 재빌드 규칙이 적용되지 않는 작업이라는 뜻입니다 — #266이
(a) "빌드 타임에 굽기"를 접은 이유가 이것이고, 덤으로 키가 저장소에 커밋되는 일도 없습니다.

이 파일이 `nginx.conf`가 아니라 `nginx.conf.template`인 것이 그 대가입니다. compose가
`/etc/nginx/templates/`에 마운트하고, `nginx:alpine`의 기동 스크립트가 envsubst로
`${FINUS_API_KEY}`를 치환해 `/etc/nginx/conf.d/default.conf`를 만듭니다. 그래서:

- **키를 바꾸면 이 컨테이너도 다시 만들어야** 합니다(`docker compose up -d backend frontend`).
  치환은 기동 시점에 한 번뿐이라, backend만 재시작하면 대시보드가 낡은 키를 계속 보냅니다.
- 키에 `$`·`"`를 넣으면 치환 결과가 nginx 문법에 섞입니다. `"`는 **컨테이너가 뜨지 않는**
  것으로 끝나지만, `$`는 실재하는 nginx 변수 이름이 뒤따르면(`abc$host`) 조용히 그 값으로
  바뀌어 다른 키가 나갑니다. `;`·`,`·공백은 nginx는 통과시키지만 쿠키 값에 쓸 수 없어
  브라우저가 값을 끊어 보냅니다. backend가 기동 로그에 미리 경고합니다.
- 쿠키는 `/`와 `/index.html` 응답에만 실립니다. 정적 자산까지 실으면 번들 하나를 받는 동안
  같은 `Set-Cookie`가 요청 수십 개에 따라붙고, 앞단에 캐시를 두는 순간 키를 품은 응답이
  저장됩니다.
- 그 두 응답에는 `Cache-Control: no-cache`가 붙습니다. **이게 없으면 방식 자체가 성립하지
  않습니다** — 쿠키는 세션 쿠키(브라우저를 닫으면 사라짐)인데 문서가 브라우저 캐시에서
  나오면 새 쿠키를 받을 기회가 없어, 다음 방문에 401이 됩니다. 키를 바꿨을 때도 같습니다.
  재검증 응답(304)에도 `Set-Cookie`가 실리므로(실측) 본문을 다시 받지는 않습니다.
  번들 자산에는 걸지 않습니다 — 53MB짜리 `.wasm`은 캐시가 동작해야 하는 쪽입니다.

쿠키 속성은 `Path=/; SameSite=Strict; HttpOnly`이고(HTTPS로 열면 `Secure`가 붙습니다),
**`SameSite=Strict`는 장식이 아닙니다.** 쿠키 인증은 브라우저가 자격증명을 알아서 붙이므로
CSRF가 따라오는데, Strict가 cross-site 요청에서 쿠키를 떼기 때문에 임의 페이지의
`fetch(..., { mode: "no-cors" })`가 인증 없는 요청이 되어 401에서 끝납니다 — CORS로는 막지
못했던 그 경로입니다. 이 값을 내리는 것은 인증 방식을 바꾸는 일입니다.

단 "site"는 **스킴 + 등록가능도메인**이고 포트는 보지 않습니다. 같은 호스트의 다른 포트에
페이지가 뜨면 그건 same-site라 쿠키가 그대로 실립니다. 지금은 전 인터페이스에 게시되는
서비스가 이 `frontend`(8080) 하나뿐이고 나머지는 루프백 전용이라(#246·#285) 그런 페이지가
없지만, 새 서비스를 전 인터페이스에 게시할 때 함께 볼 일입니다.

**WebSocket 키가 URL에 실리던 문제(#355)는 사라졌습니다.** 2단계는 `?api_key=...` 쿼리
파라미터를 썼고 그게 nginx 액세스 로그에 평문으로 남았는데, 쿠키가 핸드셰이크에도 붙게 되면서
그 경로를 걷어냈습니다. 남은 헤더 경로는 기본 `log_format`(combined)에 들어가지 않습니다.

`/openapi.json`·`/docs`는 `/api/` 접두사 밖이라 인증을 켜도 열려 있습니다. 8080에서는
`location /`의 `try_files ... =404`가 막지만, **8000에 직접 닿으면 전체 스키마가 그대로
나옵니다.** 스키마는 저장소 소스 그 자체이고 8000은 루프백 전용이라(#246) 함께 닫지
않았습니다 — 인증이 지키는 것은 "무엇이 있는지"가 아니라 "무엇을 호출할 수 있는지"입니다.

> ⚠️ **페이지를 열 수 있는 사람은 API도 부를 수 있습니다.** 정적 키가 브라우저에 도달하는
> 순간 성립하는 성질이고, 키를 어떤 방식으로 들여보내도 같습니다(#266의 2026-09-04 코멘트).
> 없애려면 로그인(세션 토큰)이나 네트워크 레벨 인증이 필요하고, 이 배포에서 브라우저 경계를
> 맡는 것은 Tailscale입니다. 즉 이 키가 실제로 막는 것은 **대시보드를 열 수 없는 비브라우저
> 호출자**입니다.

이 배선은 `backend/tests/test_nginx_api_key_cookie.py`가 고정합니다 — CI의 `nginx -t`는
문법만 보므로 `SameSite=Strict` 한 줄이 사라져도 통과합니다.

현재 번들은 비압축입니다(`Build/`에 `.br`·`.gz` 산출물 없음).

압축은 텍스트 자산(`.css`, `.js`)에만 겁니다. **`.wasm`과 `.data`는 일부러 제외했습니다** —
nginx가 동적 gzip을 걸면 `Content-Length`를 지우고 chunked로 보내는데, `Build.loader.js`가
그 헤더로 수신 버퍼를 잡기 때문에 로딩바가 멈춘 것처럼 보입니다(PR #243 리뷰 2번).
대신 `gzip_static on`을 켜 두었으므로, Unity 빌드 설정을 압축 산출물로 바꿔 `.gz`를 함께
커밋하면 `Content-Length`를 유지한 채 그대로 서빙됩니다. `.br`까지 쓰려면 `Content-Encoding`
매핑을 별도로 추가해야 합니다.
