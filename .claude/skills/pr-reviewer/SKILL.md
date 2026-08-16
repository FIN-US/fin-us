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

기술적으로 정확하게, 짧게 리뷰한다. 지적마다 코드에서 확인한 근거를 단다.
완충 문장·서론·요약 반복을 쓰지 않는다.

## 리뷰 대상 판단

**원격 PR**: PR 번호(#42) 또는 URL이 주어지면 전용 worktree에서 리뷰한다. 병렬 실행 가능하다.

**로컬 변경사항**: PR 언급이 없으면 현재 git 변경사항이 대상이다. 커밋되지 않은 변경은 메인
워킹 트리에만 있어 격리할 수 없으므로 **병렬 실행이 불가능하다.** 동시에 여러 개를 요청받으면
순차 실행하고 그 사실을 알린다.

## 병렬 실행 규칙 (원격 PR 모드)

**시작 전에 [`.claude/skills/_shared/worktree-conventions.md`](../_shared/worktree-conventions.md)를
읽는다.** worktree 생성·경로·정리, 리터럴 기록, `git -C`/`gh -R` 호출 규약이 거기 있다.
아래는 이 스킬 고유의 규칙이다.

1. **`gh pr checkout`을 쓰지 않는다.** 공유 워킹 트리의 브랜치를 갈아치우므로, 병렬 실행 시
   서로의 체크아웃을 빼앗아 매 실행마다 다른 코드를 보게 된다. 워킹 트리를 바꾸는 다른
   명령도 같은 이유로 금지다.
2. **`FETCH_HEAD`를 참조하지 않는다.** 레포당 하나뿐이라 동시 fetch가 서로를 덮어쓴다.
   PR마다 고유한 named ref로 fetch한다.

1단계에서 확정해 기록할 리터럴: `REPO`, `WT_PATH`, `REF_NAME`, `OWNER`, `BASE`.

## 워크플로

### 1. 사전 준비 — 원격 PR인 경우

```bash
git rev-parse --show-toplevel        # → <REPO>
```

base 레포와 base 브랜치를 **PR에서 직접 조회해 고정**한다. base를 `main`으로 가정하지 않는다.

```bash
gh repo view --json nameWithOwner -q .nameWithOwner       # → <OWNER>
gh pr view <PR번호> -R "<OWNER>" --json baseRefName,headRefOid,title,url
```

remote가 여러 개면 공통 규약 "origin과 gh가 같은 레포인지"를 따른다. 어긋나면 fetch한 코드와
`gh`로 읽은 메타데이터가 서로 다른 레포의 것이 된다.

조회 결과로 `<PR>`, `<BASE>`(baseRefName), `<SHA7>`(headRefOid 앞 7자)를 확정해 기록한다.

```bash
#   REF_NAME = refs/pr-review/<PR>-<SHA7>
#   WT_PATH  = <이 실행의 scratchpad 디렉토리>/rv-<PR>-<SHA7>
# 아래는 한 셸 호출에 이어 붙여 실행한다.

# PR head를 고유 named ref로 가져온다 (FETCH_HEAD 미사용 → 병렬 안전)
git -C "<REPO>" fetch origin "pull/<PR>/head:refs/pr-review/<PR>-<SHA7>"
git -C "<REPO>" fetch origin "+<BASE>:refs/remotes/origin/<BASE>" --quiet
git -C "<REPO>" worktree add --detach "<WT_PATH>" "refs/pr-review/<PR>-<SHA7>"
```

refspec 선두의 `+`, `--detach`, `WT_PATH` 조건은 공통 규약을 따른다. scratchpad를 받지
못했다면 `<REPO의 부모>/.fin-us-worktrees/rv-<PR>-<SHA7>`를 쓴다.

메타데이터와 diff 조회는 워킹 트리와 무관하므로 `-R`로 호출한다.

```bash
gh pr view <PR> -R "<OWNER>"                                   # 설명, 라벨, 리뷰어
gh pr view <PR> -R "<OWNER>" --comments                        # 기존 리뷰 코멘트
git -C "<WT_PATH>" diff "origin/<BASE>...<REF_NAME>" --stat    # 변경 범위
git -C "<WT_PATH>" diff "origin/<BASE>...<REF_NAME>"           # 전체 diff
git -C "<WT_PATH>" log "origin/<BASE>..<REF_NAME>" --oneline
```

세 점(`...`)은 merge-base 기준이라 base가 앞서 나가도 이 PR이 추가한 변경만 나온다.
**`<BASE>`를 `main`으로 하드코딩하지 않는다.** 스택형 PR이나 릴리스 브랜치 대상 PR에서는
merge-base가 엉뚱한 곳에 잡혀 무관한 커밋이 리뷰 대상 diff에 섞인다.

파일을 직접 읽어야 하면 **반드시 `<WT_PATH>` 아래 경로**로 읽는다.

### 1-b. 사전 준비 — 로컬 변경사항인 경우

worktree를 만들지 않고 메인 트리를 **읽기 전용**으로 조회한다. 대상이 메인 워킹 트리인 것은
의도이지만, 그것과 프로세스의 현재 디렉토리에 의존하는 것은 다른 문제라 여기서도 `-C`를 쓴다.

```bash
git rev-parse --show-toplevel              # → <REPO>
git -C "<REPO>" diff HEAD                  # 커밋되지 않은 전체 변경사항
git -C "<REPO>" diff --staged              # 스테이징된 변경사항
git -C "<REPO>" log --oneline -10          # 최근 커밋 이력
```

### 2. 프리플라이트 체크

새 worktree에는 gitignore 대상인 의존성 디렉토리가 없다. 이 레포의 해당 디렉토리는
`backend/.venv`, `finus_nat/.venv`, 그리고 `mcp-dart`·`mcp-news`·`mcp-trading` 각각의
`node_modules`다.

**이 목록은 `.github/workflows/ci.yml`의 잡과 맞춰 둔다.** 좁게 적어두면 CI가 검증하는 영역을
리뷰가 조용히 건너뛴다(실제로 `mcp-trading`이 그랬다). node 패키지가 추가되면 둘을 함께 갱신한다.

#### 절대 실행하지 않는 명령

- `npm install`, `pip install` — 느리고 병렬 실행 시 캐시 경합
- **`uv sync`, `--no-sync` 없는 `uv run`** — 아래 이유로 가장 위험하다

#### 파이썬 영역(`backend`, `finus_nat`)은 기본적으로 건너뛴다

`.venv`를 링크한 뒤 `uv run`을 돌리면 **개발자의 메인 트리 venv를 직접 mutate한다**
(`uv run`이 프로젝트 환경을 자동 동기화하며 링크를 따라간다). 결과는 둘 — 병렬 리뷰가 같은
venv를 동시에 건드리고, `finus_nat/.venv`의 `patch_vendor.py` 벤더 패치가 재설치로
**조용히 벗겨진다.**

패치 적용 여부는 `finus_nat/.venv/Lib/site-packages/.finus_vendor_patch.json` 마커로 확인한다.
**확인만 하고 `patch_vendor.py`를 실행하지 않는다** — 이 스크립트는 점검이 아니라 적용이라,
돌리는 순간 venv를 바꾼다(실측: 미적용 상태에서 돌렸더니 3개 파일에 패치가 적용됐다).

굳이 파이썬 검증이 필요하면 sync를 막고 인터프리터를 직접 지목한다.

```bash
# sync 없이, 링크된 venv를 읽기만 한다
"<REPO>/backend/.venv/Scripts/python.exe" -m pytest "<WT_PATH>/backend/tests/"   # Windows
"<REPO>/backend/.venv/bin/python" -m pytest "<WT_PATH>/backend/tests/"           # macOS/Linux
# uv를 쓴다면 --no-sync 필수
uv run --no-sync --project "<WT_PATH>/backend" pytest
```

worktree에서 `pytest`나 `ruff check .`를 venv 활성화 없이 그냥 실행하면 해석되지 않는다.

#### Node 영역(`mcp-dart`, `mcp-news`, `mcp-trading`)은 링크 후 실행 가능

`node_modules`는 실행만으로 재설치되지 않아 링크해도 안전하다.
**diff가 건드린 패키지만** 검증한다.

**Windows/Git Bash에서 `ln -s`를 쓰지 않는다.** `MSYS=winsymlinks`가 없으면 `ln -s`는
실패하지 않고 **디렉토리를 통째로 복사한다**(실측: 원본에 파일을 추가해도 사본에 반영되지
않았다). 성공한 것처럼 보여 폴백이 발화하지 않고, 리뷰마다 `node_modules` 전체를 복사한다.

```bash
# <PKG>는 이 PR이 실제로 건드린 패키지로 치환한다. 여러 개면 각각 반복한다.
# Windows / Git Bash
cmd //c mklink //J "<WT_PATH>\<PKG>\node_modules" "<REPO>\<PKG>\node_modules"
# macOS / Linux
ln -s "<REPO>/<PKG>/node_modules" "<WT_PATH>/<PKG>/node_modules"

# 링크가 디렉토리로 해석되는 경우에만 검증한다.
[ -d "<WT_PATH>/<PKG>/node_modules" ] \
  && npm --prefix "<WT_PATH>/<PKG>" test \
  || echo "PREFLIGHT SKIP: <PKG> node_modules 링크 실패 — 리포트에 사유를 명시할 것"
```

> `frontend/`는 Unity 프로젝트라 node 의존성이 없다. 검증 지점은 추적 중인 WebGL
> 번들(`frontend/Build/`)과 소스가 어긋나지 않았는지다(`frontend/README.md` 재빌드 규칙).

> ⚠️ **링크를 걸었으면 6단계 전에 반드시 직접 끊는다.** `git worktree remove --force`는
> junction을 **따라 들어가 원본을 지운다.** 실측했다 — `mcp-dart/node_modules`의 파일 98개가
> 0개가 됐고 `npm ci`로 복구해야 했다. 이 스킬에 오래 "링크 자체만 제거한다"고 적혀 있었으나
> 거짓이다.
>
> ```bash
> cmd //c rmdir "<WT_PATH>\<PKG>\node_modules"   # Windows: junction만 끊는다(원본 보존, 실측 확인)
> rm "<WT_PATH>/<PKG>/node_modules"              # macOS/Linux: 심볼릭 링크만 지운다
> ```
>
> 끊은 뒤 원본이 남아 있는지 확인하고 나서 6단계로 간다.

#### 건너뛴 경우

의존성 링크에 실패했거나, 설치된 의존성이 없거나, 파이썬 영역이라 건너뛴 경우 리포트에
"프리플라이트: 건너뜀 (사유)"를 명시한다. 조용히 생략하면 검증을 통과한 것처럼 읽힌다.

### 3. 컨텍스트 파악

- PR 설명과 기존 리뷰 코멘트를 읽어 목적과 히스토리 파악
- 변경된 파일의 범위와 영향도 분석
- `references/review-checklist.md`를 로드해 적용

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

주 독자는 사람이 아니라 다음 작업을 이어받는 에이전트다. 설득이 아니라 지목이 목적이다.

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

- **지적 하나는 3줄이 기본.** 문제·근거·수정 각 한 줄. 안 되는 지적만 예외로 늘린다.
- **코드 인용은 diff에 없는 것만.** 독자도 diff를 이미 본다. 수정 제안이 3줄을 넘으면
  코드 대신 산문으로 쓴다.
- **건수가 0인 섹션은 헤더째 생략한다.** "🔵 Nitpicks (0건) — 없음"은 정보가 아니다.
- **"잘 된 점" 섹션을 쓰지 않는다.** 칭찬이 필요하면 결론 줄에 한 구절로 붙인다.
- **Improvements 최대 7건, Nitpicks 최대 5건.** 넘으면 반복되는 패턴 하나로 묶는다.
- **Critical에는 건수 상한을 두지 않는다.** 분량을 이유로 잘라낼 대상이 아니다.

### 6. 정리 (원격 PR 모드 — 실패해도 반드시 수행)

**2단계에서 건 의존성 링크가 있으면 먼저 끊는다**(2단계 경고 참조). 링크를 남긴 채
`worktree remove`를 부르면 원본 `node_modules`가 날아간다.

이어서 공통 규약 "정리"를 수행하고 **임시 ref 삭제를 추가로** 한다.

```bash
git -C "<REPO>" update-ref -d "refs/pr-review/<PR>-<SHA7>"
git -C "<REPO>" for-each-ref refs/pr-review/    # 실제로 사라졌는지 확인
```

> 두 실행이 같은 PR·같은 커밋을 동시에 보면 `REF_NAME`이 겹친다. **ref는 무해하다** —
> 같은 커밋을 다시 쓰는 것이고, `--detach`라 ref가 먼저 지워져도 worktree는 유효하다.
> **경로는 다르다** — 공통 규약 "경로 충돌"을 따른다. 여기서 nonce를 되살리지 않는다.

## 행동 원칙

- 지적이 하나도 없으면 결론 한 줄로 끝낸다 — 빈 섹션을 나열하지 않는다
- 프리플라이트를 건너뛰었으면 리포트에 사유를 한 줄로 명시한다
- 추측성 비판 금지 — 코드에서 직접 근거를 찾는다
- 한국어로 작성한다 (코드 예시는 영어 유지)
