# worktree 공통 규약

`pr-creator`와 `pr-reviewer`가 공유한다. 실패 양상이 같아서 한쪽만 고치면 조용히 어긋난다.

## 왜 worktree인가

여러 실행이 같은 워킹 트리를 공유하면 서로가 만든 인덱스/HEAD 변화를 "변경사항"으로 오인해
매번 다른 diff를 본다. worktree는 `.git`만 공유하고 인덱스와 HEAD는 분리된다.

## 경로 충돌

worktree를 이 실행에 주어진 scratchpad 아래 두면 경로가 **세션마다** 고유하므로 다른
세션과는 겹치지 않는다. 이름에 nonce를 섞거나 남의 잔재를 훑는 스윕을 돌릴 이유가 없다.

**단, 한 세션 안에서는 고유하지 않다.** 같은 세션의 병렬 서브에이전트 둘이 같은 대상을
동시에 처리하면 경로가 완전히 같아진다. 그래서 `worktree add` 직전에 확인한다.

```bash
git -C "<REPO>" worktree list --porcelain    # <WT_PATH>가 이미 있는지 본다
```

이미 있으면 `<WT_PATH>`에 `-2`(그다음은 `-3`)를 붙인 **새 리터럴을 기록하고** 이후 모든
단계에서 그것을 쓴다. 확인을 통과했더라도 `worktree add`가 `fatal: '<경로>' already exists`로
죽을 수 있다(확인과 생성 사이에 다른 실행이 먼저 만든 경우). 이때도 접미사를 올려 재시도한다.

> **`worktree add`에 성공하지 못했다면 그 경로는 이 실행의 것이 아니다.** 기록해 둔
> `<WT_PATH>`를 정리 단계에서 지우면 그 경로를 실제로 소유한 다른 실행의 worktree가 날아간다.
> 정리는 **내가 만든 worktree에만** 한다.

## 현재 디렉토리에 의존하지 않기

- `cd`, `git checkout`, `git switch`, `git stash`를 쓰지 않는다.
- git은 `git -C "<REPO>"`, gh는 `gh ... -R "<OWNER>"` 형태로 호출한다.
- `<REPO>`를 얻는 `git rev-parse --show-toplevel` 한 줄만 현재 디렉토리에 의존하는 부트스트랩이다.

## 셸 변수는 호출 간에 살아남지 않는다

셸 호출마다 새 프로세스다. 앞 단계에서 정한 `$WT`를 정리 단계에서 참조하면 빈 문자열이 되어
`git worktree remove --force ""`가 되고, 정리가 조용히 실패해 worktree가 누적된다.

확정한 값(`REPO`, `WT_PATH`, `OWNER`, `BASE` 등)은 **리터럴로 출력해 기록**하고, 이후 모든
단계에서 그 리터럴 문자열을 직접 쓴다. 여러 명령을 한 셸 호출에 이어 붙였더라도 기록은 한다.

## WT_PATH

`WT_PATH`는 **레포 바깥**이어야 한다. 레포 안에 두면 소스 트리 전체 사본이 생겨, 누군가
루트에서 범위 넓은 명령(pytest 수집, 전체 grep, 린트)을 돌릴 때 중첩 사본까지 훑는다.
특히 pytest는 같은 이름의 테스트 모듈이 두 벌 잡히면 `import file mismatch`로 죽는다.
`.gitignore`에 걸려 git 상태가 깨끗한 것과는 별개다.

scratchpad는 레포 바깥이면서 세션마다 고유해 두 조건을 만족한다. 받지 못했다면
`<REPO의 부모>/.fin-us-worktrees/` 아래에 만든다.

## worktree 생성

```bash
# base는 refspec을 명시한다. --single-branch 클론에서는 refs/remotes/origin/<BASE>가
# 갱신되지 않아 이후 origin/<BASE> 참조가 unknown revision으로 죽는다.
# 선두의 '+'는 필수다. 없으면 base가 rebase/force-push된 뒤 non-fast-forward로 거부된다.
git -C "<REPO>" fetch origin "+<BASE>:refs/remotes/origin/<BASE>" --quiet
git -C "<REPO>" worktree add --detach "<WT_PATH>" "<대상 ref>"
```

`--detach`를 뺄 수 없다. git은 같은 브랜치를 두 worktree에서 동시에 체크아웃하지 못해,
메인 디렉토리나 다른 병렬 실행이 그 브랜치를 쓰고 있으면 실패한다.

`worktree add`와 `fetch`는 `.git/worktrees`·`packed-refs` 락 경합으로 실패할 수 있다.
실패하면 몇 초 후 1회 재시도하고, 그래도 실패하면 사용자에게 보고한다.

## 정리 (실패해도 반드시 수행)

기록해 둔 리터럴 경로를 쓴다. 셸 변수를 참조하지 않는다.
**`worktree add`에 성공한 경우에만** 수행한다("경로 충돌" 참조).

```bash
# 1차: 정상 경로
git -C "<REPO>" worktree remove --force "<WT_PATH>"
# 실패했다면 폴백 — 경로가 이 실행의 <WT_PATH>가 맞는지 확인한 뒤에만 실행한다
rm -rf "<WT_PATH>"
git -C "<REPO>" worktree prune
git -C "<REPO>" worktree list          # 실제로 사라졌는지 확인
```

**`worktree remove`가 실패하면 반드시 폴백을 실행한다.** Windows에서 프로세스가 파일을
잡고 있으면 흔히 실패하고, `worktree prune`은 디렉토리가 남아 있는 한 등록을 유지해 회수하지
못한다(실측 확인). 이 정리가 유일한 회수 지점이라 건너뛰면 누수가 영구히 남는다.

중간에 오류가 나거나 사용자가 취소해도 제거한다. 정리에 실패하면 `<WT_PATH>`를 사용자에게
알려 수동으로 지울 수 있게 한다.

## origin과 gh가 같은 레포인지

remote가 여러 개면 `origin`이 `<OWNER>`와 같은 레포인지 확인한다.
**단순 문자열 비교는 하지 않는다** — 레포가 전송/이름변경되면 origin URL은 옛 이름이고 `gh`는
새 이름으로 해석해 정상 상태에서도 어긋나 보인다(이 레포가 그렇다: origin `sorocode/fin-us`,
gh `FIN-US/fin-us`). `gh api "repos/<owner/repo>" --jq .id`로 양쪽 id를 비교하고, 다르면
**경고만** 하고 계속할지 사용자에게 확인한다 — 오탐 여지가 있다.
