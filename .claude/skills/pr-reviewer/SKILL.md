---
name: pr-reviewer
description: >
  GitHub Pull Request를 전문적으로 리뷰합니다.
  PR 번호(예: "PR #42 리뷰해줘"), PR URL,
  또는 "내 변경사항 리뷰해줘"라고 하면 자동으로 발동됩니다.
  코드 품질, 보안, 성능, 테스트 커버리지를 분석합니다.
---

# PR Reviewer Skill

## 원칙

기술적으로 정확하게, 짧게 리뷰합니다. 지적마다 코드에서 확인한 근거를 답니다.
완충 문장·서론·요약 반복은 쓰지 않습니다 — 지적과 근거만 남깁니다.

## 리뷰 대상 판단

**원격 PR**: PR 번호(#42) 또는 URL이 주어지면 해당 PR을 대상으로 합니다.
→ 전용 worktree에서 리뷰합니다. **병렬 실행 가능**합니다.

**로컬 변경사항**: PR 언급이 없으면 현재 git 변경사항을 대상으로 합니다.
→ 커밋되지 않은 변경은 메인 워킹 트리에만 존재하므로 worktree로 격리할 수 없습니다.
**이 모드는 병렬 실행이 불가능합니다.** 동시에 여러 개를 요청받으면 순차 실행하고 그 사실을 알립니다.

## 병렬 실행 규칙 (원격 PR 모드)

**시작 전에 [`.claude/skills/_shared/worktree-conventions.md`](../_shared/worktree-conventions.md)를
읽습니다.** worktree 생성·경로·정리, 리터럴 기록, `git -C`/`gh -R` 호출 규약은 전부 거기에 있습니다.
아래는 이 스킬에만 해당하는 규칙입니다.

1. **`gh pr checkout`을 절대 사용하지 않습니다.** 이 명령은 공유 워킹 트리의 브랜치를 갈아치웁니다.
   병렬 실행 시 서로의 체크아웃을 빼앗아 매 실행마다 다른 코드를 보게 되는 원인입니다.
2. **`FETCH_HEAD`를 참조하지 않습니다.** `.git/FETCH_HEAD`는 레포당 하나뿐이라 동시 fetch가 서로를 덮어씁니다.
   PR마다 고유한 named ref로 fetch합니다.

1단계에서 확정해 기록할 리터럴은 `REPO`, `WT_PATH`, `REF_NAME`, `OWNER`, `BASE`입니다.
기록을 빠뜨리면 6단계에서 worktree와 `refs/pr-review/*`가 리뷰할 때마다 누적됩니다.

## 워크플로

### 1. 사전 준비 — 원격 PR인 경우

먼저 `<REPO>`를 확정합니다. 이 한 줄만 현재 디렉토리에 의존하는 부트스트랩이며,
이후 모든 git 호출은 여기서 얻은 리터럴로 `git -C "<REPO>"` 형태를 씁니다.

```bash
git rev-parse --show-toplevel        # → <REPO>
```

base 레포와 base 브랜치를 **PR에서 직접 조회해 고정**합니다. base를 `main`으로 가정하지 않습니다.

```bash
gh repo view --json nameWithOwner -q .nameWithOwner       # → <OWNER>
gh pr view <PR번호> -R "<OWNER>" --json baseRefName,headRefOid,title,url
```

remote가 여러 개면 공통 규약의 "origin과 gh가 같은 레포인지"를 따릅니다. 어긋나면 fetch한
코드와 `gh`로 읽은 메타데이터가 서로 다른 레포의 것이 됩니다.

위 조회 결과로 리터럴 값을 확정하고 **출력해 기록**합니다 —
`<PR>`, `<BASE>`(baseRefName), `<SHA7>`(headRefOid 앞 7자).

```bash
# 아래 <...>는 확정한 리터럴로 치환해 실행하고, 그 결과 경로/ref를 반드시 기록해 둡니다.
#   REF_NAME = refs/pr-review/<PR>-<SHA7>
#   WT_PATH  = <이 실행의 scratchpad 디렉토리>/rv-<PR>-<SHA7>
# 아래 명령들은 한 셸 호출에 이어 붙여 실행한다.

# PR head를 고유 named ref로 가져온다 (FETCH_HEAD 미사용 → 병렬 안전)
git -C "<REPO>" fetch origin "pull/<PR>/head:refs/pr-review/<PR>-<SHA7>"
git -C "<REPO>" fetch origin "+<BASE>:refs/remotes/origin/<BASE>" --quiet
git -C "<REPO>" worktree add --detach "<WT_PATH>" "refs/pr-review/<PR>-<SHA7>"
```

base refspec 선두의 `+`가 왜 필수인지, `--detach`가 왜 핵심인지, `WT_PATH`를 왜 레포 바깥에
두는지는 공통 규약을 따릅니다. scratchpad를 받지 못했다면
`<REPO의 부모>/.fin-us-worktrees/rv-<PR>-<SHA7>`를 씁니다.

메타데이터와 diff 조회는 워킹 트리와 무관하므로 `-R`로 안전하게 호출합니다.

```bash
gh pr view <PR> -R "<OWNER>"                                   # 설명, 라벨, 리뷰어
gh pr view <PR> -R "<OWNER>" --comments                        # 기존 리뷰 코멘트
git -C "<WT_PATH>" diff "origin/<BASE>...<REF_NAME>" --stat    # 변경 범위
git -C "<WT_PATH>" diff "origin/<BASE>...<REF_NAME>"           # 전체 diff
git -C "<WT_PATH>" log "origin/<BASE>..<REF_NAME>" --oneline
```

세 점(`...`)은 merge-base 기준이라 base가 앞서 나가도 이 PR이 실제로 추가한 변경만 보여줍니다.
**`<BASE>`를 `main`으로 하드코딩하지 마세요.** 스택형 PR이나 릴리스 브랜치 대상 PR에서는
merge-base가 엉뚱한 곳에 잡혀 이 PR과 무관한 커밋이 리뷰 대상 diff에 섞여 들어옵니다.

파일 내용을 직접 읽어야 하면 **반드시 `<WT_PATH>` 아래 경로**로 읽습니다. 메인 작업 디렉토리 경로로 읽지 않습니다.

### 1-b. 사전 준비 — 로컬 변경사항인 경우

worktree를 만들지 않고 메인 트리를 **읽기 전용**으로 조회합니다. 상태를 바꾸지 않습니다.

이 모드도 `<REPO>`를 먼저 확정해 `-C`로 호출합니다. 대상이 메인 워킹 트리인 것은 의도이지만,
그것과 *프로세스의 현재 디렉토리에 의존하는 것*은 다른 문제입니다.

```bash
git rev-parse --show-toplevel              # → <REPO> (부트스트랩)
git -C "<REPO>" diff HEAD                  # 커밋되지 않은 전체 변경사항
git -C "<REPO>" diff --staged              # 스테이징된 변경사항
git -C "<REPO>" log --oneline -10          # 최근 커밋 이력
```

### 2. 프리플라이트 체크

새 worktree에는 gitignore 대상인 의존성 디렉토리가 없습니다. 이 레포의 해당 디렉토리는
`backend/.venv`, `finus_nat/.venv`, 그리고 **`mcp-dart`·`mcp-news`·`mcp-trading` 각각의
`node_modules`** 입니다.

**대상 목록은 `.github/workflows/ci.yml`의 잡과 맞춰 둡니다.** 목록을 좁게 적어두면 CI가
검증하는 영역을 리뷰가 조용히 건너뜁니다(실제로 `mcp-trading`이 그랬습니다).
node 패키지가 추가되면 이 목록과 CI 잡을 함께 갱신합니다.

#### 절대 실행하지 않는 명령

- `npm install`, `pip install` — 느리고 병렬 실행 시 캐시 경합
- **`uv sync`, 그리고 `--no-sync` 없는 `uv run`** — 아래 이유로 가장 위험합니다

#### 파이썬 영역(`backend`, `finus_nat`)은 기본적으로 건너뜁니다

`.venv`를 링크한 뒤 `uv run`을 돌리면 **개발자의 메인 트리 venv를 직접 mutate합니다**
(`uv run`이 프로젝트 환경을 자동 동기화하며 링크를 따라갑니다). 결과는 둘입니다 —
병렬 리뷰가 같은 venv를 동시에 건드리고, `finus_nat/.venv`에 적용된 `patch_vendor.py`
벤더 패치가 재설치로 **조용히 벗겨져 로컬 NAT 환경이 깨집니다.**

굳이 파이썬 검증이 필요하면 sync를 막고 인터프리터를 직접 지목합니다.

```bash
# sync 없이, 링크된 venv를 읽기만 한다
"<REPO>/backend/.venv/bin/python" -m pytest "<WT_PATH>/backend/tests/"
# uv를 쓴다면 --no-sync 필수
uv run --no-sync --project "<WT_PATH>/backend" pytest
```

참고: 이 레포의 CI가 쓰는 실제 명령은 `uv sync --project backend` / `uv run --project backend pytest`
입니다. `pytest`나 `ruff check .`를 worktree에서 venv 활성화 없이 그냥 실행하면 해석되지 않습니다.

#### Node 영역(`mcp-dart`, `mcp-news`, `mcp-trading`)은 링크 후 실행 가능

`node_modules`는 실행만으로 재설치되지 않으므로 링크해도 안전합니다.
**diff가 건드린 패키지만** 검증합니다 — 안 바뀐 패키지까지 돌릴 이유가 없습니다.

```bash
# <PKG>는 이 PR이 실제로 건드린 패키지로 치환한다. 여러 개면 각각 반복한다.
ln -s "<REPO>/<PKG>/node_modules" "<WT_PATH>/<PKG>/node_modules" \
  || cmd //c mklink //J "<WT_PATH>/<PKG>/node_modules" "<REPO>/<PKG>/node_modules"
# ln -s는 Windows에서 개발자 모드가 꺼져 있고 MSYS=winsymlinks도 미설정이면 실패한다.
# 그 경우 junction(mklink //J)으로 폴백한다.

# 링크가 실제로 디렉토리로 해석되는지 확인하고, 성공한 경우에만 검증을 실행한다.
# 확인을 && 로 묶어 실패 시 npm이 실행되지 않도록 한다.
[ -d "<WT_PATH>/<PKG>/node_modules" ] \
  && npm --prefix "<WT_PATH>/<PKG>" test \
  || echo "PREFLIGHT SKIP: <PKG> node_modules 링크 실패 — 리포트에 사유를 명시할 것"
```

> `frontend/`는 Unity 프로젝트라 node 의존성이 없습니다. 검증 지점은 추적 중인 WebGL
> 번들(`frontend/Build/`)과 소스가 어긋나지 않았는지입니다(`frontend/README.md` 재빌드 규칙).

> 6단계의 `worktree remove --force`는 링크 자체만 제거하고 원본 `node_modules`는 보존합니다
> (심볼릭 링크·junction 모두 실측 확인). 링크를 지울까 걱정해 정리를 건너뛰지 마세요.

#### 건너뛴 경우

**의존성 링크에 실패했거나, 설치된 의존성이 없거나, 파이썬 영역이라 건너뛴 경우**
리포트에 "프리플라이트: 건너뜀 (사유)"를 명시합니다. 조용히 생략하면 검증을 통과한 것처럼
읽히므로 금지입니다.

### 3. 컨텍스트 파악

- PR 설명과 기존 리뷰 코멘트를 읽어 목적과 히스토리 파악
- 변경된 파일의 범위와 영향도 분석
- `references/review-checklist.md`를 로드하여 체크리스트 기준 적용

### 4. 분석 기준 (우선순위 순)

**🔴 Critical** — 반드시 수정 필요

- 버그 및 로직 오류
- 보안 취약점 (SQL Injection, XSS, 인증 누락 등)
- 브레이킹 체인지 (하위 호환성 파괴)
- 데이터 손실 가능성

**🟡 Improvements** — 강력 권장

- 명백한 성능 병목 (N+1 쿼리, 불필요한 루프 등)
- 에러 핸들링 누락
- 테스트 커버리지 부족
- 코드 중복 (DRY 원칙 위반)

**🔵 Nitpicks** — 선택적 개선

- 네이밍 컨벤션
- 코멘트/문서화
- 마이너 스타일 이슈

### 5. 리뷰 리포트 형식

주 독자는 사람이 아니라 다음 작업을 이어받는 에이전트입니다. 설득이 아니라 지목이 목적입니다.

```
## 리뷰: <PR 제목>
<변경의 목적과 범위 1~2줄>

### 🔴 Critical (<n>건)
- `파일:라인` — <무엇이 잘못됐는지 한 줄>
  근거: <코드에서 확인한 사실 한 줄>
  수정: <제안 한 줄, 또는 코드 1~3줄>

### 🟡 Improvements (<n>건)
### 🔵 Nitpicks (<n>건)

### 🏁 결론
Approved ✅ / Request Changes 🔄 / Comment 💬 — <이유 한 줄>
```

#### 분량 규칙

- **지적 하나는 3줄이 기본입니다.** 문제·근거·수정 각 한 줄. 이걸로 안 되는 지적만 예외를 씁니다.
- **코드 인용은 diff에 없는 것만.** 리뷰어와 독자 모두 diff를 이미 봅니다. 변경된 줄을 그대로
  다시 붙이는 것은 분량만 늘립니다. 수정 제안도 3줄을 넘기면 산문으로 대체합니다.
- **건수가 0인 섹션은 헤더째 생략합니다.** "🔵 Nitpicks (0건) — 없음"은 정보가 아닙니다.
- **"잘 된 점" 섹션은 쓰지 않습니다.** 칭찬이 필요하면 결론 줄에 한 구절로 붙입니다.
- **Improvements 최대 7건, Nitpicks 최대 5건.** 넘으면 반복되는 패턴 하나로 묶습니다.

### 6. 정리 (원격 PR 모드 — 실패해도 반드시 수행)

공통 규약의 "정리"를 수행하고, **임시 ref 삭제를 추가로** 합니다.

```bash
git -C "<REPO>" update-ref -d "refs/pr-review/<PR>-<SHA7>"
```

> 두 실행이 같은 PR·같은 커밋을 동시에 보면 `REF_NAME`이 겹칩니다. **ref는 무해합니다** —
> 같은 커밋을 다시 쓰는 것이고, `--detach`라 ref가 먼저 지워져도 worktree는 유효합니다.
> **경로는 다릅니다** — 공통 규약 "경로 충돌"을 따르세요. 여기서 nonce를 되살리지 마세요.

정리 후 실제로 사라졌는지 확인합니다.

```bash
git -C "<REPO>" worktree list
git -C "<REPO>" for-each-ref refs/pr-review/
```

중간 단계에서 오류가 나거나 리뷰를 중단하더라도 worktree와 임시 ref는 제거합니다.
정리에 실패하면 `<WT_PATH>`와 ref 이름을 사용자에게 알려 수동으로 지울 수 있게 합니다.

## 행동 원칙

- 지적이 하나도 없으면 결론 한 줄로 끝냅니다 — 빈 섹션을 나열하지 않습니다
- 프리플라이트를 건너뛴 경우 반드시 리포트에 사유와 함께 명시 (한 줄)
- 추측성 비판 금지 — 코드에서 직접 근거를 찾아야 함
- 한국어로 리뷰 작성 (코드 예시는 영어 유지)
