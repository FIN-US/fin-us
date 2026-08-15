# worktree 공통 규약

`pr-creator`와 `pr-reviewer`가 공유하는 규약입니다. 두 스킬 모두 격리된 worktree에서
동작하고 실패 양상도 같아서, 한쪽만 고치면 조용히 어긋나므로 여기 한 벌만 둡니다.

## 왜 worktree인가

두 스킬은 여러 PR에 대해 동시에 실행될 수 있습니다. 여러 실행이 같은 워킹 트리를 공유하면
서로가 만든 인덱스/HEAD 변화를 "변경사항"으로 오인해 매번 다른 diff를 보게 됩니다.
worktree는 `.git`은 공유하되 인덱스와 HEAD는 분리되므로, 실행마다 독립된 스냅샷을 갖습니다.

**경로 충돌은 대부분 실행 환경이 막습니다.** worktree를 이 실행에 주어진 scratchpad
디렉토리 아래 두면 그 경로가 **세션마다** 고유하므로, 다른 세션과는 절대 겹치지 않습니다.
이름에 nonce를 섞거나 남의 잔재를 훑는 스윕을 돌릴 이유가 없습니다.

## 경로 충돌

**scratchpad는 한 세션 안에서는 고유하지 않습니다.** 같은 세션의 병렬 서브에이전트 둘이 이
스킬을 동시에 돌리면 scratchpad가 같습니다. 이름에 브랜치명/PR 번호가 들어가므로 **대상이
다르면 겹치지 않지만**, 같은 대상을 둘이 동시에 처리하면 경로가 완전히 같아집니다.

그래서 **`worktree add` 직전에 그 경로가 이미 쓰이는지 확인합니다.**

```bash
git -C "<REPO>" worktree list --porcelain    # <WT_PATH>가 이미 있는지 본다
```

이미 있으면 `<WT_PATH>`에 `-2`(그다음은 `-3`)를 붙인 **새 리터럴을 기록하고** 이후 모든
단계에서 그것을 씁니다.

**확인을 통과했더라도 `worktree add`가 `already exists`로 실패할 수 있습니다.** 확인과 생성
사이에 다른 실행이 먼저 만든 경우입니다(실측: 같은 경로에 두 번째 `worktree add`는
`fatal: '<경로>' already exists`로 죽습니다). 이때는 접미사를 올려 다시 시도합니다.

> **`worktree add`에 성공하지 못했다면 그 경로는 이 실행의 것이 아닙니다.** 정리 단계에서
> 절대 건드리지 마세요. 실패한 실행이 기록해 둔 `<WT_PATH>`를 지우면, 그 경로를 실제로
> 소유한 다른 실행의 worktree가 날아갑니다. 정리는 **내가 만든 worktree에만** 합니다.

## 현재 디렉토리에 의존하지 않기

- `cd`, `git checkout`, `git switch`, `git stash`는 사용하지 않습니다.
- git은 `git -C "<REPO>"`, gh는 `gh ... -R "<OWNER>"` 형태로 호출합니다.
- `<REPO>`를 얻는 `git rev-parse --show-toplevel` 한 줄만 현재 디렉토리에 의존하는
  부트스트랩입니다.

## 셸 변수는 호출 간에 살아남지 않습니다

스킬을 실행하는 에이전트는 **셸 호출마다 새 프로세스**를 씁니다. 환경변수는 유지되지 않습니다.
앞 단계에서 정한 `$WT`를 정리 단계에서 그대로 참조하면 빈 문자열이 되어
`git worktree remove --force ""`가 되고, 정리가 조용히 실패해 worktree가 누적됩니다.

따라서 확정한 값(`REPO`, `WT_PATH`, `OWNER`, `BASE` 등)은 **리터럴로 출력해 기록**하고,
이후 모든 단계에서 셸 변수가 아니라 그 리터럴 문자열을 직접 사용합니다.
여러 명령을 한 셸 호출에 이어 붙여도 되지만, 그렇게 했다는 이유로 리터럴 기록을 생략하지 않습니다.

## WT_PATH

`WT_PATH`는 **레포 바깥**이어야 합니다. 레포 안에 두면 소스 트리 전체 사본이 생겨, 누군가
루트에서 범위 넓은 명령(pytest 수집, 전체 grep, 린트)을 돌릴 때 중첩 사본까지 훑습니다.
특히 pytest는 같은 이름의 테스트 모듈이 두 벌 잡히면 `import file mismatch`로 죽습니다.
`.gitignore`에 걸려 git 상태가 깨끗한 것과는 별개의 문제입니다.

scratchpad 디렉토리는 레포 바깥이면서 세션마다 고유하므로 두 조건을 모두 만족합니다.
scratchpad를 받지 못했다면 `<REPO의 부모>/.fin-us-worktrees/` 아래에 만듭니다.

## worktree 생성

```bash
# base는 refspec을 명시한다. --single-branch 클론에서는 refs/remotes/origin/<BASE>가
# 갱신되지 않아 이후 origin/<BASE> 참조가 unknown revision으로 죽는다.
# 선두의 '+'는 필수다. 없으면 base가 rebase/force-push된 뒤 non-fast-forward로 거부되어
# ! [rejected] (non-fast-forward)로 죽는다. 기본 refspec에 '+'가 있어서 원래는 없던 실패 모드다.
git -C "<REPO>" fetch origin "+<BASE>:refs/remotes/origin/<BASE>" --quiet
git -C "<REPO>" worktree add --detach "<WT_PATH>" "<대상 ref>"
```

`--detach`가 핵심입니다. git은 같은 브랜치를 두 worktree에서 동시에 체크아웃할 수 없어서,
`--detach` 없이는 메인 디렉토리나 다른 병렬 실행과 충돌합니다.

**`worktree add`와 `fetch`는 락 경합으로 실패할 수 있습니다.** 병렬 실행에서 `.git/worktrees`와
`packed-refs` 락이 경합합니다. 실패하면 몇 초 후 1회 재시도하고, 그래도 실패하면 사용자에게 보고합니다.

## 정리 (실패해도 반드시 수행)

**기록해 둔 리터럴 경로를 그대로 씁니다. 셸 변수를 참조하지 마세요.**
그리고 **`worktree add`에 성공한 경우에만** 수행합니다("경로 충돌" 참조).

```bash
# 1차: 정상 경로
git -C "<REPO>" worktree remove --force "<WT_PATH>"
# 실패했다면 폴백 — 경로가 이 실행의 <WT_PATH>가 맞는지 확인한 뒤에만 실행한다
rm -rf "<WT_PATH>"
git -C "<REPO>" worktree prune
git -C "<REPO>" worktree list          # 실제로 사라졌는지 확인
```

**`worktree remove`가 실패하면 반드시 폴백을 실행합니다.** Windows에서 프로세스가 파일을
잡고 있으면 흔히 실패하고, `worktree prune`은 디렉토리가 남아 있는 한 등록을 유지하므로
회수하지 못합니다(실측 확인). 이 정리가 유일한 회수 지점이므로, 건너뛰면 누수가 그대로 남습니다.

중간에 오류가 나거나 사용자가 취소하더라도 worktree는 제거합니다. 정리에 실패하면
`<WT_PATH>`를 사용자에게 알려 수동으로 지울 수 있게 합니다.

## origin과 gh가 같은 레포인지

remote가 여러 개면 `git -C "<REPO>" remote -v`로 `origin`이 `<OWNER>`와 같은 레포인지 확인합니다.
**단순 문자열 비교는 하지 마세요** — 레포가 전송/이름변경된 경우 origin URL은 옛 이름이고 `gh`는
새 이름으로 해석해 정상 상태에서도 어긋나 보입니다(이 레포가 그렇습니다: origin은
`sorocode/fin-us`, `gh`는 `FIN-US/fin-us`). `gh api "repos/<owner/repo>" --jq .id`로 양쪽 id를
비교하고, 다르면 **경고만** 하고 계속할지 사용자에게 확인합니다 — 오탐 여지가 있습니다.
