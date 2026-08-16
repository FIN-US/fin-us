---
name: pr-creator
description: "PR을 생성합니다. 'PR 올려줘', 'PR 만들어줘'라고 하면 발동됩니다."
---

# PR Creator Skill

## 필수 규칙

**시작 전에 [`.claude/skills/_shared/worktree-conventions.md`](../_shared/worktree-conventions.md)를
읽는다.** worktree 생성·경로·정리, 리터럴 기록, `git -C`/`gh -R` 호출 규약이 거기 있다.
아래는 이 스킬 고유의 규칙이다.

1. `.github/PULL_REQUEST_TEMPLATE.md`를 반드시 쓴다. **개요 / 발견된 문제 / 문제 해결 방법 /
   검증 / 관련 이슈** 다섯 섹션이고, 아래 분량 규칙이 이 구성을 전제한다.
   섹션을 늘리거나 줄이지 않는다.
2. **모든 작업을 전용 worktree에서 한다.** 메인 워킹 트리는 읽지도 건드리지도 않는다.
3. **커밋 범위로만 변경사항을 파악한다.** `git status`, 인자 없는 `git diff`, `HEAD` 기준
   비교처럼 워킹 트리 상태에 의존하는 명령을 쓰지 않는다.
4. `git add`, `git commit`을 쓰지 않는다. 커밋은 이미 되어 있다는 전제다.

0단계에서 확정해 기록할 리터럴: `REPO`, `WT_PATH`, `OWNER`, `BASE`, `BRANCH`.

## 워크플로

### 0. 값 확정

```bash
git rev-parse --show-toplevel          # → <REPO>
git -C "<REPO>" branch --show-current  # → <BRANCH> (사용자가 지정하지 않은 경우에만)
gh repo view --json nameWithOwner -q .nameWithOwner              # → <OWNER>
gh repo view --json defaultBranchRef -q .defaultBranchRef.name   # → <BASE> 기본값
```

`<BASE>`를 `main`으로 하드코딩하지 않는다. 사용자가 지정하지 않았으면 `defaultBranchRef`
조회값을 쓴다.

remote가 여러 개면 공통 규약의 "origin과 gh가 같은 레포인지"를 따른다. 어긋나면 push한
브랜치와 `gh pr create`가 바라보는 레포가 달라진다.

> **규칙 2·3의 예외:** 브랜치를 지정받지 못했을 때만 `git branch --show-current`로 공유 HEAD를
> 읽는다. 규칙 2·3이 금지한 가변 상태이지만 대상 브랜치를 알아낼 다른 방법이 없다.
> **여기서 딱 한 번만 읽고** 이후로는 확정된 리터럴만 쓴다. 사용자가 브랜치를 지정했다면
> 이 읽기 자체를 생략한다.

```
SLUG     = <BRANCH의 "/"를 "-"로 치환>
WT_PATH  = <이 실행의 scratchpad 디렉토리>/pr-<SLUG>
```

`WT_PATH` 조건과 scratchpad를 못 받았을 때의 폴백은 공통 규약 "WT_PATH"를 따른다.

### 1. 격리된 worktree 생성

공통 규약 "worktree 생성"대로 하되 대상 ref는 로컬 브랜치다.

```bash
git -C "<REPO>" fetch origin "+<BASE>:refs/remotes/origin/<BASE>" --quiet
git -C "<REPO>" worktree add --detach "<WT_PATH>" "<BRANCH>"
```

`<BRANCH>`는 fetch하지 않는다. PR 생성 전이라 원격에 없는 것이 정상이고,
`fetch origin "<BRANCH>"`는 `couldn't find remote ref`로 실패한다.

### 2. 템플릿 로드

```bash
cat "<WT_PATH>/.github/PULL_REQUEST_TEMPLATE.md"
```

### 3. 변경사항 파악 (커밋 범위 기준)

```bash
git -C "<WT_PATH>" log "origin/<BASE>..<BRANCH>" --oneline
git -C "<WT_PATH>" diff "origin/<BASE>...<BRANCH>" --stat
git -C "<WT_PATH>" diff "origin/<BASE>...<BRANCH>"
```

세 점(`...`)은 merge-base 기준이라 base가 앞서 나가도 이 브랜치가 추가한 변경만 나온다.

파일을 직접 읽어야 하면 `<WT_PATH>` 아래 경로로 읽는다. worktree에는 `node_modules`,
`.venv`, `.env`가 없지만 이 스킬은 빌드·테스트를 돌리지 않으므로 상관없다.

### 4. 템플릿 기반으로 본문 작성

로드한 템플릿의 네 섹션을 유지하면서 3단계 분석 결과로 채운다.

#### 분량 규칙

주 독자는 사람이 아니라 이 PR을 읽을 에이전트다. 설득이 아니라 확인이 목적이다.
**평서체로 쓴다**(`~했다`, `~이다`). 존댓말 종결어미를 쓰지 않는다.
**문장을 임의로 줄바꿈하지 않는다** — 마크다운은 단일 개행을 무시하고, PR 본문은 버전 관리
대상이 아니라 diff 가독성 이득도 없다. 한 문단은 한 줄로 쓴다.

**PR 페이지가 이미 보여주는 것을 본문에 옮기지 않는다.** 변경된 파일 목록과 커밋 이력은
Files changed 탭과 커밋 목록에 있다. 본문에는 **거기서 읽을 수 없는 것**만 쓴다 — 왜
문제였는지, 어떤 접근으로 풀었는지, 그리고 코드에 남지 않는 검증 결과.

| 섹션 | 담는 것 | 담지 않는 것 |
| --- | --- | --- |
| 발견된 문제 | 증상과 영향. **코드를 몰라도 이해되게** | 파일명, 함수명 |
| 문제 해결 방법 | 접근 방식 (개념 수준) | 파일 목록, 변경 나열 |
| 검증 | **CI가 잡지 않는 것만** — 수동 재현, 실측 수치 | CI가 돌리는 테스트 결과 |

- **다섯 섹션만 쓴다.** 섹션을 새로 만들지 않는다.
- `## 개요`는 **2줄 이내**로 아래를 요약한다.
- `## 발견된 문제`와 `## 문제 해결 방법`은 **번호를 맞춘다.** 1번 문제 ↔ 1번 해결.
  각 **한 항목 2줄 이내**, 최대 5개. 넘으면 PR을 쪼갤 때다.
- **diff에 있는 코드를 본문에 옮기지 않는다.** 코드 인용은 diff만 봐서는 안 보이는 것
  (제거된 동작, 호출 관계, 재현 절차)일 때만 3줄 이내로 쓴다.
- `## 검증`은 명령과 결과를 한 줄씩. CI가 전부 덮으면 "CI 통과" 한 줄이거나 생략한다.
  `pytest 469 passed`처럼 PR 체크에 초록불로 뜨는 것은 중복이다.
- 알려진 한계나 범위 밖 항목은 `## 개요` 마지막에 **각 한 줄**로 붙인다. 없으면 생략한다.
- UI가 바뀌는 변경(`frontend/`)이면 스크린샷을 `## 개요`에 붙인다. 전용 섹션은 없다.

### 5. 사용자 확인

제목과 본문을 보여주고 확인받는다. 확인 없이 올리지 않는다.

### 6. 브랜치 푸시 + PR 생성

```bash
git -C "<REPO>" push origin "<BRANCH>:<BRANCH>"

gh pr create \
  -R "<OWNER>" \
  --head "<BRANCH>" \
  --base "<BASE>" \
  --title "<제목>" \
  --body-file "<본문 파일 경로>"
```

`-R`과 `--head`를 명시하면 gh가 현재 디렉토리의 git 상태를 추론하지 않는다.
`<OWNER>`는 0단계에서 기록한 리터럴을 쓴다 — 여기서 `gh repo view`를 다시 부르면 0단계에서
origin과 대조해 확인한 값과 달라질 수 있다.
본문은 셸 이스케이프 사고를 피해 파일에 쓰고 `--body-file`로 넘긴다.

### 7. 정리 (실패해도 반드시 수행)

공통 규약 "정리"를 수행한다. 5단계에서 사용자가 취소했거나 중간에 오류가 나도 실행한다.
