---
name: pr-creator
description: "PR을 생성합니다. 'PR 올려줘', 'PR 만들어줘'라고 하면 발동됩니다."
---

# PR Creator Skill

## 필수 규칙

**시작 전에 [`.claude/skills/_shared/worktree-conventions.md`](../_shared/worktree-conventions.md)를
읽습니다.** worktree 생성·경로·정리, 리터럴 기록, `git -C`/`gh -R` 호출 규약은 전부 거기에 있습니다.
아래는 이 스킬에만 해당하는 규칙입니다.

1. PR 생성 시 반드시 `.github/PULL_REQUEST_TEMPLATE.md` 템플릿을 사용합니다.
   템플릿은 **개요 / 변경 사항 / 검증 / 관련 이슈** 네 섹션이고, 이 스킬의 분량 규칙은
   이 네 섹션을 전제로 합니다. 섹션을 늘리거나 줄이지 않습니다.
2. **모든 작업은 전용 git worktree 안에서 수행합니다.** 메인 작업 디렉토리의 워킹 트리는
   절대 읽지도 건드리지도 않습니다.
3. **커밋 범위(commit range) 기반으로만 변경사항을 파악합니다.** `git status`,
   `git diff`(인자 없이), `HEAD` 기준 비교처럼 워킹 트리 상태에 의존하는 명령은 금지입니다.
4. `git add`, `git commit`은 사용하지 않습니다. 커밋은 이미 되어 있다는 전제입니다.

0단계에서 확정해 기록할 리터럴은 `REPO`, `WT_PATH`, `OWNER`, `BASE`, `BRANCH`입니다.

## 워크플로

### 0. 값 확정

```bash
git rev-parse --show-toplevel          # → <REPO> (현재 디렉토리에 의존하는 유일한 부트스트랩)
git -C "<REPO>" branch --show-current  # → <BRANCH> (사용자가 지정하지 않은 경우에만)
gh repo view --json nameWithOwner -q .nameWithOwner       # → <OWNER>
gh repo view --json defaultBranchRef -q .defaultBranchRef.name   # → <BASE> 기본값
```

`<REPO>`를 얻는 첫 줄만 현재 디렉토리에 의존합니다. 이후 모든 git 호출은 `git -C "<REPO>"` 형태를 씁니다.

`<BASE>`를 리터럴 `main`으로 하드코딩하지 않습니다. 사용자가 지정하지 않았으면 위의
`defaultBranchRef`로 조회한 값을 씁니다.

remote가 여러 개면 공통 규약의 "origin과 gh가 같은 레포인지"를 따릅니다. 어긋나면 push한
브랜치와 `gh pr create`가 바라보는 레포가 달라집니다.

> **규칙 2·3의 예외:** 브랜치를 지정받지 못했을 때만 `git branch --show-current`로 공유 HEAD를
> 읽습니다. 이는 규칙 2·3이 금지한 가변 상태이지만, 대상 브랜치를 알아낼 다른 방법이 없어
> 허용하는 예외입니다. **여기서 딱 한 번만 읽고**, 이후 단계에서는 확정된 리터럴 브랜치명만
> 사용합니다. 사용자가 브랜치를 지정했다면 이 읽기 자체를 생략합니다.

확정 결과로 리터럴을 기록합니다.

```
SLUG     = <BRANCH의 "/"를 "-"로 치환>
WT_PATH  = <이 실행의 scratchpad 디렉토리>/pr-<SLUG>
```

`WT_PATH`가 왜 레포 바깥이어야 하는지, scratchpad를 못 받았을 때 어디에 두는지는
공통 규약의 "WT_PATH"를 따릅니다.

### 1. 격리된 worktree 생성

공통 규약의 "worktree 생성"대로 하되, 대상 ref는 로컬 브랜치입니다.

```bash
git -C "<REPO>" fetch origin "+<BASE>:refs/remotes/origin/<BASE>" --quiet
git -C "<REPO>" worktree add --detach "<WT_PATH>" "<BRANCH>"
```

`<BRANCH>`는 fetch하지 않습니다. PR을 올리기 전이라 원격에 아직 없는 것이 정상이며,
`fetch origin "<BRANCH>"`는 `couldn't find remote ref`로 실패합니다.
worktree는 로컬 브랜치 ref에서 바로 만듭니다.

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

세 점(`...`)은 merge-base 기준 비교라서 base 브랜치가 앞서 나가도 이 브랜치가 실제로 추가한
변경만 보여줍니다. 워킹 트리를 전혀 참조하지 않으므로 병렬 실행에 안전합니다.

파일 내용을 직접 읽어야 하면 `<WT_PATH>` 아래 경로로 읽습니다. 메인 작업 디렉토리 경로로 읽지 않습니다.
(worktree에는 `node_modules`, `.venv`, `.env` 같은 gitignore 대상이 없습니다. 이 스킬은 빌드나
테스트를 실행하지 않으므로 문제되지 않습니다.)

### 4. 템플릿 기반으로 본문 작성

로드한 템플릿 형식을 그대로 유지하면서 3단계에서 분석한 변경사항으로 내용을 채웁니다.
템플릿 구조(섹션, 체크리스트 등)를 절대 변경하지 않습니다.

#### 분량 규칙

**본문의 주 독자는 사람이 아니라 이 PR을 읽을 에이전트입니다.** 리뷰어를 설득하는 글이 아니라
무엇을 왜 바꿨는지 확인시키는 글이므로, 아래를 지킵니다.

- **템플릿의 네 섹션만 씁니다.** 섹션을 새로 만들지 않습니다.
- **`## 개요`는 4줄 이내**, `## 변경 사항`은 **항목당 한 줄**로 최대 7개.
- **diff에 있는 코드를 본문에 옮기지 않습니다.** 코드 인용은 diff만 봐서는 안 보이는 것
  (제거된 동작, 호출 관계, 재현 절차)일 때만, 3줄 이내로 씁니다.
- **`## 검증`은 실행한 명령과 결과를 한 줄씩.** `pytest backend/ — 469 passed, 2 skipped`
  형태입니다. 뮤테이션 표·시도별 기록처럼 재현 가능한 과정은 넣지 않습니다.
- 같은 사실을 두 섹션에 쓰지 않습니다.
- 알려진 한계나 범위 밖 항목은 `## 개요` 마지막에 **각 한 줄**로 붙입니다. 없으면 생략합니다.

글자 수 상한은 두지 않습니다 — 셀 수 없는 기준은 지켜지지 않습니다. 위의 섹션·줄·항목 수가
실제 상한입니다.

### 5. 사용자 확인

생성할 PR 제목과 본문을 먼저 보여주고 확인받습니다. 확인 없이 바로 올리지 않습니다.

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

`-R`과 `--head`를 명시하면 gh가 현재 디렉토리의 git 상태를 추론하지 않습니다.
`<OWNER>`는 0단계에서 확정해 기록한 리터럴을 씁니다 — 여기서 `gh repo view`를 다시 호출하면
0단계에서 origin과 대조해 확인한 값과 달라질 수 있습니다.
본문은 셸 이스케이프 사고를 피하기 위해 임시 파일에 쓰고 `--body-file`로 전달합니다.

### 7. 정리 (실패해도 반드시 수행)

공통 규약의 "정리"를 그대로 수행합니다. 5단계에서 사용자가 취소했거나 중간에 오류가 나도
반드시 실행합니다.

중간 단계에서 오류가 나거나 사용자가 5단계에서 취소하더라도 worktree는 제거합니다.
정리에 실패하면 `<WT_PATH>`를 사용자에게 알려 수동으로 지울 수 있게 합니다.
