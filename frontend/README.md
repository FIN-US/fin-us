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

**`frontend/Assets/Scripts/`의 C# 소스를 수정한 경우, Unity 에디터에서 WebGL 재빌드 후 `frontend/Build/`를 함께 커밋해야 합니다.**
소스만 커밋하고 빌드를 갱신하지 않으면, 추적 중인 번들과 C# 소스가 어긋납니다.

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

현재 레포 구성에는 `frontend/Build/`를 자동으로 서빙하는 경로가 없습니다 (이슈 #212 조사 결과).

| 확인 대상 | 결과 |
| --- | --- |
| `docker-compose.yml`의 `frontend` 서비스 | `./frontend-react` 빌드(React 앱). Unity `frontend/`가 아님 |
| `frontend-react` 소스·`index.html`·`public/` | `Build.loader`/`Build.data`/iframe 참조 **0건** |
| `.github/workflows/ci.yml` | `frontend/Build`·`frontend/Assets`·unity 언급 **0건** |
| 저장소 전체 `StaticFiles`/`mount(` grep | Unity 번들 서빙 지점 **0건** |

이는 "서빙하지 않으므로 추적 불필요"를 뜻하지 않습니다. 현재는 팀원 로컬 실행 목적으로 추적하며, 배포 파이프라인이 추가될 때 이 문서를 갱신하세요.
