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
5. **셸 변수가 호출 간에 살아남는다고 가정하지 않습니다.** 아래 "값 확정과 기록" 참조.
6. **`worktree add`와 `fetch`는 락 경합으로 실패할 수 있습니다.** 병렬 실행에서 `.git/worktrees`와
   `packed-refs` 락이 경합합니다. 실패하면 몇 초 후 1회 재시도하고, 그래도 실패하면 사용자에게 보고합니다.

### 왜 이렇게 하는가 (병렬 실행 안전성)

이 스킬은 여러 PR에 대해 동시에 실행될 수 있습니다. 여러 실행이 같은 워킹 트리를 공유하면
서로가 만든 인덱스/HEAD 변화를 "변경사항"으로 오인해 매번 다른 diff를 보게 됩니다.
worktree는 `.git`은 공유하되 인덱스와 HEAD는 분리되므로, 실행마다 독립된 스냅샷을 갖습니다.

## 값 확정과 기록 (중요)

이 스킬을 실행하는 에이전트는 **셸 호출마다 새 프로세스**를 씁니다. 환경변수는 호출 간에 유지되지 않고,
`$$`(PID)는 호출마다 다른 값이 됩니다. 0단계에서 정한 `$WT`를 7단계 정리에서 그대로 참조하면
빈 문자열이 되어 정리가 조용히 실패하고, worktree가 PR을 올릴 때마다 누적됩니다.

**복구 가능성을 주는 것은 아래의 "리터럴 기록"이지 이름의 결정성이 아닙니다.**
따라서 이름은 고유해야 하고, 동시에 기록되어야 합니다.

- 이름에 **nonce를 반드시 포함**합니다: `pr-<SLUG>-<SHA7>-<nonce>`.
  결정적 이름만 쓰면 같은 브랜치를 같은 커밋에서 두 번 동시에 처리할 때 경로가 완전히 같아지고,
  7단계의 `worktree remove --force`가 **아직 작업 중인 다른 실행의 worktree를 통째로 지웁니다.**
- `<nonce>`는 0단계에서 **한 번만** 만들어 리터럴로 기록합니다. `$$`를 쓰지 않습니다 —
  호출마다 달라지는 것이 원래 문제였습니다.
- 0단계에서 확정한 **`REPO`, `WT_PATH`, `OWNER`, `BASE`, `BRANCH`의 리터럴 값을 출력해 기록**하고,
  이후 모든 단계에서는 셸 변수가 아니라 그 리터럴 문자열을 직접 사용합니다.

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

remote가 여러 개면 `git -C "<REPO>" remote -v`로 `origin`이 `<OWNER>`와 같은 레포인지 확인합니다.
**단순 문자열 비교는 하지 마세요** — 레포가 전송/이름변경된 경우 origin URL은 옛 이름이고 `gh`는
새 이름으로 해석하므로 정상 상태에서도 어긋나 보입니다(이 레포가 실제로 그렇습니다:
origin은 `sorocode/fin-us`, `gh`는 `FIN-US/fin-us`). 레포 동일성은 id로 비교합니다.

```bash
gh api "repos/<origin URL의 owner/repo>" --jq .id      # vs
gh api "repos/<OWNER>" --jq .id
```

id가 다르면 push한 브랜치와 `gh pr create`가 바라보는 레포가 달라지므로 **경고하고** 사용자에게
계속할지 확인합니다. 중단이 아니라 경고입니다 — 오탐 여지가 있습니다.

> **규칙 2·3의 예외:** 브랜치를 지정받지 못했을 때만 `git branch --show-current`로 공유 HEAD를
> 읽습니다. 이는 규칙 2·3이 금지한 가변 상태이지만, 대상 브랜치를 알아낼 다른 방법이 없어
> 허용하는 예외입니다. **여기서 딱 한 번만 읽고**, 이후 단계에서는 확정된 리터럴 브랜치명만
> 사용합니다. 사용자가 브랜치를 지정했다면 이 읽기 자체를 생략합니다.

확정 결과로 리터럴을 기록합니다.

```
SLUG     = <BRANCH의 "/"를 "-"로 치환>
SHA7     = git -C "<REPO>" rev-parse --short=7 <BRANCH>
nonce    = 이 실행에서 한 번만 생성한 6자 영숫자
WT_PATH  = <REPO의 부모>/.fin-us-worktrees/pr-<SLUG>-<SHA7>-<nonce>
```

worktree는 **레포 바깥**에 둡니다. 레포 안에 두면 소스 트리 전체 사본이 생겨, 누군가 루트에서
범위 넓은 명령(pytest 수집, 전체 grep, 린트)을 돌릴 때 중첩 사본까지 훑습니다. 특히 pytest는
같은 이름의 테스트 모듈이 두 벌 잡히면 `import file mismatch`로 죽습니다.

### 1. 격리된 worktree 생성

먼저 stale 스윕으로 이전 실행이 남긴 누수를 회수합니다. `pr-reviewer`와 `.fin-us-worktrees`
루트를 공유하므로, **`pr-*` 디렉토리만** 대상으로 합니다(`rv-*`는 pr-reviewer의 것입니다).

```bash
git -C "<REPO>" worktree prune
WTROOT="<REPO의 부모>/.fin-us-worktrees"
# 등록된 worktree가 아닌 pr-* 잔여 디렉토리를 회수한다.
# 디렉토리 mtime은 우리가 만든 시각이라 신뢰할 수 있다. 1시간 미만은 진행 중일 수 있으므로 제외.
find "$WTROOT" -mindepth 1 -maxdepth 1 -type d -name 'pr-*' -mmin +60 2>/dev/null | while read -r d; do
  git -C "<REPO>" worktree list --porcelain | grep -qF "$d" && continue
  rm -rf "$d"
done
git -C "<REPO>" worktree prune
```

이어서 이 실행의 worktree를 만듭니다.

```bash
# base는 refspec을 명시한다. --single-branch 클론에서는 refs/remotes/origin/<BASE>가
# 갱신되지 않아 3단계의 origin/<BASE> 참조가 unknown revision으로 죽는다.
# 선두의 '+'는 필수다. 없으면 base가 rebase/force-push된 뒤 non-fast-forward로 거부되어
# ! [rejected] (non-fast-forward)로 죽는다. 기본 refspec에 '+'가 있어서 원래는 없던 실패 모드다.
git -C "<REPO>" fetch origin "+<BASE>:refs/remotes/origin/<BASE>" --quiet
git -C "<REPO>" worktree add --detach "<WT_PATH>" "<BRANCH>"
```

`<BRANCH>`는 fetch하지 않습니다. PR을 올리기 전이라 원격에 아직 없는 것이 정상이며,
`fetch origin "<BRANCH>"`는 `couldn't find remote ref`로 실패합니다.
worktree는 로컬 브랜치 ref에서 바로 만듭니다.

`--detach`가 핵심입니다. git은 같은 브랜치를 두 worktree에서 동시에 체크아웃할 수 없기 때문에,
`--detach` 없이는 메인 디렉토리나 다른 병렬 실행이 이미 그 브랜치를 쓰고 있을 때 실패합니다.
detached HEAD는 브랜치 커밋의 읽기 전용 스냅샷이며 PR 생성에는 이것으로 충분합니다.

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

**0단계에서 기록해 둔 리터럴 경로를 그대로 씁니다. 셸 변수를 참조하지 마세요** —
이 호출은 0단계와 다른 프로세스라 변수가 비어 있고, `git worktree remove --force ""`가 되어
정리가 조용히 실패합니다.

```bash
# 1차: 정상 경로
git -C "<REPO>" worktree remove --force "<WT_PATH>"
# 실패했다면 폴백 — 경로가 <WTROOT> 하위인지 확인한 뒤에만 실행한다
rm -rf "<WT_PATH>"
git -C "<REPO>" worktree prune
git -C "<REPO>" worktree list          # 실제로 사라졌는지 확인
```

**`worktree remove`가 실패하면 반드시 폴백을 실행합니다.** Windows에서 프로세스가 파일을
잡고 있으면 흔히 실패하고, `worktree prune`은 디렉토리가 남아 있는 한 등록을 유지하므로
회수하지 못합니다(실측 확인). 1단계 스윕은 1시간이 지나야 회수하므로 그때까지 누수가 남습니다.

`<WT_PATH>`는 **이 실행의 nonce가 붙은 것**입니다. 다른 실행의 worktree를 지우지 않도록
0단계에서 기록한 리터럴을 그대로 씁니다.

중간 단계에서 오류가 나거나 사용자가 5단계에서 취소하더라도 worktree는 제거합니다.
정리에 실패하면 `<WT_PATH>`를 사용자에게 알려 수동으로 지울 수 있게 합니다.
