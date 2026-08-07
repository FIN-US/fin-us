---
name: pr-creator
description: "PR을 생성합니다. 'PR 올려줘', 'PR 만들어줘'라고 하면 발동됩니다."
---

# PR Creator Skill

## 필수 규칙

1. PR 생성 시 반드시 `.github/PULL_REQUEST_TEMPLATE.md` 템플릿을 사용합니다.
2. **모든 작업은 전용 git worktree 안에서 수행합니다.** 메인 작업 디렉토리의 워킹 트리는
   절대 읽지도 건드리지도 않습니다.
3. **커밋 범위(commit range) 기반으로만 변경사항을 파악합니다.** `git status`,
   `git diff`(인자 없이), `HEAD` 기준 비교처럼 워킹 트리 상태에 의존하는 명령은 금지입니다.
4. `cd`, `git checkout`, `git switch`, `git stash`, `git add`, `git commit`은 사용하지 않습니다.
   git은 `git -C <경로>`로, gh는 `gh ... -R <owner/repo>`로 호출해 프로세스의 현재 디렉토리에
   전혀 의존하지 않게 만듭니다.

### 왜 이렇게 하는가 (병렬 실행 안전성)

이 스킬은 여러 PR에 대해 동시에 실행될 수 있습니다. 여러 실행이 같은 워킹 트리를 공유하면
서로가 만든 인덱스/HEAD 변화를 "변경사항"으로 오인해 매번 다른 diff를 보게 됩니다.
worktree는 `.git`은 공유하되 인덱스와 HEAD는 분리되므로, 실행마다 독립된 스냅샷을 갖습니다.

## 워크플로

### 0. 변수 결정

```bash
REPO="$(git -C . rev-parse --show-toplevel)"
BRANCH="<PR을 올릴 브랜치명>"          # 사용자가 지정하지 않았다면 git -C "$REPO" branch --show-current
BASE="main"
SLUG="$(echo "$BRANCH" | tr '/' '-')"
WT="$REPO/.claude/worktrees/pr-$SLUG-$$"   # $$ 로 실행마다 고유 경로 확보
```

`$WT` 경로는 **실행마다 반드시 고유**해야 합니다. 병렬 실행끼리 경로가 겹치면 격리 의미가 없습니다.
`.claude/`는 `.gitignore`에 있으므로 worktree가 메인 트리의 상태를 오염시키지 않습니다.

### 1. 격리된 worktree 생성

```bash
git -C "$REPO" fetch origin "$BASE" --quiet
git -C "$REPO" worktree add --detach "$WT" "$BRANCH"
```

`$BRANCH`는 fetch하지 않습니다. PR을 올리기 전이라 원격에 아직 없는 것이 정상이며,
`fetch origin "$BRANCH"`는 `couldn't find remote ref`로 실패합니다.
worktree는 로컬 브랜치 ref에서 바로 만듭니다.

`--detach`가 핵심입니다. git은 같은 브랜치를 두 worktree에서 동시에 체크아웃할 수 없기 때문에,
`--detach` 없이는 메인 디렉토리나 다른 병렬 실행이 이미 그 브랜치를 쓰고 있을 때 실패합니다.
detached HEAD는 브랜치 커밋의 읽기 전용 스냅샷이며 PR 생성에는 이것으로 충분합니다.

`worktree add`가 lock 충돌로 실패하면 몇 초 후 1회 재시도하고, 그래도 실패하면 사용자에게 보고합니다.

### 2. 템플릿 로드

```bash
cat "$WT/.github/PULL_REQUEST_TEMPLATE.md"
```

### 3. 변경사항 파악 (커밋 범위 기준)

```bash
git -C "$WT" log "origin/$BASE..$BRANCH" --oneline
git -C "$WT" diff "origin/$BASE...$BRANCH" --stat
git -C "$WT" diff "origin/$BASE...$BRANCH"
```

세 점(`...`)은 merge-base 기준 비교라서 base 브랜치가 앞서 나가도 이 브랜치가 실제로 추가한
변경만 보여줍니다. 워킹 트리를 전혀 참조하지 않으므로 병렬 실행에 안전합니다.

파일 내용을 직접 읽어야 하면 `$WT` 아래 경로로 읽습니다. 메인 작업 디렉토리 경로로 읽지 않습니다.
(worktree에는 `node_modules`, `.venv`, `.env` 같은 gitignore 대상이 없습니다. 이 스킬은 빌드나
테스트를 실행하지 않으므로 문제되지 않습니다.)

### 4. 템플릿 기반으로 본문 작성

로드한 템플릿 형식을 그대로 유지하면서 3단계에서 분석한 변경사항으로 내용을 채웁니다.
템플릿 구조(섹션, 체크리스트 등)를 절대 변경하지 않습니다.

### 5. 사용자 확인

생성할 PR 제목과 본문을 먼저 보여주고 확인받습니다. 확인 없이 바로 올리지 않습니다.

### 6. 브랜치 푸시 + PR 생성

```bash
git -C "$REPO" push origin "$BRANCH:$BRANCH"

gh pr create \
  -R "$(gh repo view --json nameWithOwner -q .nameWithOwner)" \
  --head "$BRANCH" \
  --base "$BASE" \
  --title "<제목>" \
  --body-file "<본문 파일 경로>"
```

`-R`과 `--head`를 명시하면 gh가 현재 디렉토리의 git 상태를 추론하지 않습니다.
본문은 셸 이스케이프 사고를 피하기 위해 임시 파일에 쓰고 `--body-file`로 전달합니다.

### 7. 정리 (실패해도 반드시 수행)

```bash
git -C "$REPO" worktree remove --force "$WT"
git -C "$REPO" worktree prune
```

중간 단계에서 오류가 나거나 사용자가 5단계에서 취소하더라도 worktree는 제거합니다.
정리에 실패하면 `$WT` 경로를 사용자에게 알려 수동으로 지울 수 있게 합니다.
