#!/usr/bin/env bash
# frontend/Assets/(Unity 소스)와 frontend/Build/(git이 추적하는 WebGL 번들)이 서로
# 맞는지 확인하기 위한 스탬프 도구다(이슈 #345).
#
# 왜 필요한가 — CI의 `unity-build-drift` 잡은 원래 "이 PR이 Assets/와 Build/를 함께
# 건드렸는가"만 봤다. 그래서 재빌드한 뒤 소스를 한 번 더 고친 상태(둘 다 변했지만
# 내용은 어긋난 상태)가 그대로 통과했고, PR #262에서 실제로 두 번 그 상태가 됐다.
# 두 번째는 문서 어긋남이 아니라 동작 버그였다 — 소스에는 실패 배너가 들어갔으나
# 번들은 옛것이라 화면에 아무것도 뜨지 않았다.
#
# 무엇을 기록하는가 — 재빌드 시점의 frontend/Assets/ **트리 해시**를 스탬프 파일에
# 적는다. git이 실제로 그 디렉터리를 커밋할 때 만드는 트리 오브젝트의 해시라, 별도
# 해시 규약을 새로 정의하지 않는다. 임시 인덱스(GIT_INDEX_FILE)에 add해서 계산하므로
# 사용자의 스테이징 상태는 건드리지 않고, 커밋 전 작업 트리 내용을 그대로 반영한다.
# 줄바꿈 정규화(autocrlf)도 git의 clean 필터를 그대로 타므로 Windows에서 계산한 값이
# Linux CI에서 다시 계산한 값과 같다.
#
# 사용법:
#   scripts/frontend_build_stamp.sh write   # 재빌드 직후 스탬프를 갱신한다
#   scripts/frontend_build_stamp.sh check   # 스탬프와 현재 Assets/를 대조한다 (CI)
#   scripts/frontend_build_stamp.sh print   # 현재 Assets/ 트리 해시만 출력한다
set -euo pipefail

SRC_DIR="frontend/Assets"
STAMP_FILE="frontend/build-stamp.txt"
STAMP_KEY="assets-tree"

usage() {
  cat >&2 <<'USAGE'
usage: scripts/frontend_build_stamp.sh <write|check|print>

  write   현재 frontend/Assets/ 트리 해시를 frontend/build-stamp.txt에 기록한다.
          Unity WebGL 재빌드 **직후에만** 실행하세요. 재빌드 없이 실행하면 어긋난
          상태에 도장을 찍는 것이라 이 검사의 의미가 사라집니다.
  check   기록된 해시와 현재 frontend/Assets/를 대조한다. 다르면 종료 코드 1.
  print   현재 frontend/Assets/ 트리 해시를 출력한다.
USAGE
}

# GitHub Actions에서는 로그 주석으로도 보이게 한다. 로컬에서는 평범한 stderr 출력.
err() {
  if [ -n "${GITHUB_ACTIONS:-}" ]; then
    printf '::error::%s\n' "$1" >&2
  else
    printf '%s\n' "$1" >&2
  fi
}

tmpdir=""
# EXIT 트랩은 아래 compute_current_tree가 중간에 실패했을 때를 위한 뒷정리다.
# 조건식 하나로 끝내면 tmpdir이 빈 상태에서 트랩의 종료 코드가 1이 되고, bash는 그 값을
# 스크립트 종료 코드로 덮어쓴다 — 성공한 실행이 실패로 보인다. 그래서 if로 감싼다.
cleanup() {
  if [ -n "$tmpdir" ]; then
    rm -rf "$tmpdir"
    tmpdir=""
  fi
}
trap cleanup EXIT

# 현재 작업 트리 기준 frontend/Assets/의 git 트리 해시를 CURRENT_TREE에 담는다.
# 값을 반환(echo) 대신 전역에 담는 이유는, 명령 치환 $( )의 서브셸에서는 EXIT 트랩이
# 상속되지 않아 실패 시 임시 디렉터리가 남기 때문이다.
CURRENT_TREE=""
compute_current_tree() {
  tmpdir=$(mktemp -d)
  # 빈 임시 인덱스에 -A로 add하면 .gitignore를 존중한 채 Assets/ 아래 전부가 담긴다.
  GIT_INDEX_FILE="$tmpdir/index" git add -A -- "$SRC_DIR"
  CURRENT_TREE=$(GIT_INDEX_FILE="$tmpdir/index" git write-tree --prefix="$SRC_DIR/")
  cleanup
}

# 스탬프 파일에서 기록된 해시를 뽑는다. 형식이 깨져 있으면 빈 문자열.
# .gitattributes가 이 파일을 eol=lf로 고정하지만, 그 규칙이 생기기 전에 CRLF로 체크아웃된
# 워킹트리도 있을 수 있어 tr로 CR을 먼저 걷어낸다. 남아 있으면 해시 끝에 \r이 붙어
# "형식이 깨졌습니다"로 잘못 진단된다.
recorded_tree() {
  tr -d '\r' < "$STAMP_FILE" | sed -n "s/^${STAMP_KEY}: \([0-9a-f]\{40\}\)\$/\1/p" | head -n 1
}

write_stamp() {
  compute_current_tree
  cat > "$STAMP_FILE" <<STAMP
# frontend/Assets/의 git 트리 해시 — 이 번들이 어느 소스에서 나왔는지 적어 둔 기록입니다.
# scripts/frontend_build_stamp.sh 가 씁니다. 손으로 고치지 마세요.
# 갱신 시점: Unity WebGL 재빌드 직후 (frontend/README.md의 재빌드 규칙 참고)
${STAMP_KEY}: ${CURRENT_TREE}
STAMP
  printf '%s 갱신: %s\n' "$STAMP_FILE" "$CURRENT_TREE"
}

check_stamp() {
  local recorded

  if [ ! -f "$STAMP_FILE" ]; then
    err "$STAMP_FILE 이 없습니다. Unity WebGL 재빌드 후 'scripts/frontend_build_stamp.sh write'로 만드세요."
    return 1
  fi

  recorded=$(recorded_tree)
  if [ -z "$recorded" ]; then
    err "$STAMP_FILE 의 형식이 깨졌습니다. '${STAMP_KEY}: <40자리 해시>' 줄이 있어야 합니다."
    return 1
  fi

  compute_current_tree
  if [ "$recorded" = "$CURRENT_TREE" ]; then
    printf '%s 은 현재 %s/ 와 일치합니다 (%s).\n' "$STAMP_FILE" "$SRC_DIR" "$CURRENT_TREE"
    return 0
  fi

  err "커밋된 번들(frontend/Build/)이 현재 $SRC_DIR/ 에서 나온 것이 아닙니다. Unity WebGL 재빌드 후 'scripts/frontend_build_stamp.sh write'를 실행하고 frontend/Build/ 와 $STAMP_FILE 을 함께 커밋하세요 (frontend/README.md의 재빌드 규칙 참고)."
  printf '  기록된 해시: %s\n' "$recorded" >&2
  printf '  현재 해시  : %s\n' "$CURRENT_TREE" >&2
  # 기록된 트리 오브젝트가 로컬에 있을 때만 차이를 보여 준다. CI 체크아웃은 얕아서
  # (fetch-depth: 2) 오래된 스탬프의 트리는 없을 수 있고, 그때 git diff는 실패한다.
  if git cat-file -e "${recorded}^{tree}" 2>/dev/null; then
    printf '  스탬프 이후 바뀐 소스 파일:\n' >&2
    git diff --name-only "$recorded" "$CURRENT_TREE" | sed "s|^|    $SRC_DIR/|" >&2
  fi
  return 1
}

main() {
  cd "$(git rev-parse --show-toplevel)"
  case "${1:-}" in
    write) write_stamp ;;
    check) check_stamp ;;
    print) compute_current_tree; printf '%s\n' "$CURRENT_TREE" ;;
    *) usage; return 2 ;;
  esac
}

main "$@"
