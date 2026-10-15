# frontend

Unity WebGL 3D 포트폴리오 대시보드. 빌드된 번들을 nginx가 서빙하고, `/api` 요청은 backend로 중계.

## 구성

```
frontend/
├── Assets/              Unity 소스
│   ├── Scripts/           C# (ApiClient, DashboardUiController, PieChart 등)
│   ├── Scenes/            씬
│   ├── UI/, UI Toolkit/   UI 레이아웃(UXML)·스타일
│   ├── Graphics/          머티리얼·셰이더
│   ├── Plugins/           WebGL 입력 플러그인 (FinUsWebInput.jslib)
│   └── WebGLTemplates/    WebGL 페이지 템플릿 (Build/index.html의 원본)
├── Packages/, ProjectSettings/   Unity 패키지·프로젝트 설정
├── Build/               WebGL 빌드 산출물 (git 추적, Unity 없이도 실행 가능하도록)
├── build-stamp.txt      Build/가 어느 Assets/에서 나왔는지 기록 (CI가 대조)
└── nginx.conf.template  nginx 설정 (정적 서빙, /api 중계, 요청 제한, API 키 쿠키)
```

## 실행

```bash
docker compose up frontend    # http://localhost:8080
```

- 번들은 API를 상대 경로(`/api/...`)로 부르므로 반드시 nginx를 거쳐 실행. 정적 서버로만 띄우면 화면만 뜨고 데이터는 연결되지 않음.
- 데이터까지 보려면 backend가 함께 떠 있어야 함 (`bash scripts/run_stack.sh`).

## 소스 수정 시 재빌드 (필수)

`Assets/`를 고쳤으면 `Build/`와 스탬프를 함께 커밋. 빠뜨리면 CI(`unity-build-drift`)가 실패.

1. Unity Hub에서 `frontend/`를 열고 **File > Build Settings**에서 **WebGL** 선택.
2. **Build**를 누르고 출력 폴더로 **`frontend/Build`** 선택 (결과가 `Build/Build/Build.wasm`처럼 이중 `Build/`가 정상).
3. 빌드 직후 스탬프 갱신: `scripts/frontend_build_stamp.sh write`
4. `git add frontend/Assets frontend/Build frontend/build-stamp.txt` 후 커밋.

- 스탬프는 반드시 **재빌드 직후에만** 갱신. 재빌드 없이 찍으면 검사가 무의미해짐.
- 스탬프는 트리 해시 기록일 뿐 "이 번들이 이 소스로 빌드됐다"를 증명하지 못함. `write --force`나, 재빌드 커밋과 소스 수정 커밋을 나눈 경우는 CI를 통과함 — CI에서 Unity 빌드를 돌리지 않는 대신 남는 위험.
- 빌드 설정을 바꿔 새 산출물 경로가 생기면 `.gitignore` 화이트리스트(`Build/index.html`, `Build/Build/**`, `Build/TemplateData/**`)도 함께 갱신.

## nginx가 하는 일

- **`/api` 중계**: `/api/`·`/health`를 `backend:8000`으로 전달. 대시보드와 API가 같은 주소(8080)라 어느 호스트로 열어도 동작.
- **요청 제한**: `/api/v1/analyze` 분당 6회·동시 2건(LLM 과금 경로), 그 밖의 `/api/`는 초당 2회. 초과 시 429.
- **API 키 쿠키**: `.env`의 `FINUS_API_KEY`가 있으면 nginx가 페이지 응답에 키를 쿠키로 실어, 번들 재빌드 없이 인증 통과. 키를 바꾸면 `docker compose up -d backend frontend`로 frontend도 다시 생성.
- Unity 에디터 플레이 모드는 nginx를 거치지 않아 키를 채운 배포에서는 401. 에디터 테스트 중에는 `FINUS_API_KEY`를 비움.
