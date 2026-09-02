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
# 무엇을 기록하는가 — 재빌드 시점의 frontend/Assets/와 frontend/Build/ **트리 해시**를
# 스탬프 파일에 적는다. git이 실제로 그 디렉터리를 커밋할 때 만드는 트리 오브젝트의
# 해시라, 별도 해시 규약을 새로 정의하지 않는다. 임시 인덱스(GIT_INDEX_FILE)에 add해서
# 계산하므로 사용자의 스테이징 상태는 건드리지 않고, 커밋 전 작업 트리 내용을 그대로
# 반영한다. 줄바꿈 정규화(autocrlf)도 git의 clean 필터를 그대로 타므로 Windows에서
# 계산한 값이 Linux CI에서 다시 계산한 값과 같다.
#
# 무엇을 보장하지 못하는가 — 이 도구는 "write를 진짜 재빌드 직후에 실행했는가"를
# 증명하지 못한다. build-tree 가드(아래 write 참고)가 가장 흔한 우회를 막지만
# --force로 넘길 수 있고, 재빌드 커밋과 소스 수정 커밋을 나누면 CI의 두 검사를 모두
# 통과할 수 있다. 그것까지 막으려면 CI에서 실제로 Unity 빌드를 돌려야 한다
# (#345의 선택지 B — 라이선스와 빌드 시간 비용 때문에 택하지 않았다).
# frontend/README.md의 "CI가 잡아 주는 것과 사람이 해야 하는 것" 참고.
#
# 사용법:
#   scripts/frontend_build_stamp.sh write [--force]  # 재빌드 직후 스탬프를 갱신한다
#   scripts/frontend_build_stamp.sh check            # 스탬프와 현재 트리를 대조한다 (CI)
#   scripts/frontend_build_stamp.sh print            # 현재 트리 해시를 출력한다
set -euo pipefail

SRC_DIR="frontend/Assets"
BUILD_DIR="frontend/Build"
STAMP_FILE="frontend/build-stamp.txt"
SRC_KEY="assets-tree"
BUILD_KEY="build-tree"

usage() {
  cat >&2 <<'USAGE'
usage: scripts/frontend_build_stamp.sh <write [--force]|check|print>

  write   현재 frontend/Assets/ 와 frontend/Build/ 트리 해시를
          frontend/build-stamp.txt에 기록한다. Unity WebGL 재빌드 **직후에만**
          실행하세요. 지난 스탬프 이후 frontend/Build/ 가 전혀 바뀌지 않았으면
          "재빌드 없이 도장만 다시 찍는" 것으로 보고 거부한다.
          --force 는 그 거부를 넘긴다 (소스를 고쳤는데 번들 바이트가 정말로
          동일하게 나온 경우에만 쓰세요).
  check   기록된 해시와 현재 트리를 대조한다. 다르면 종료 코드 1.
  print   현재 두 트리 해시를 출력한다.
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
# EXIT 트랩은 아래 compute_tree가 중간에 실패했을 때를 위한 뒷정리다.
# 조건식 하나로 끝내면 tmpdir이 빈 상태에서 트랩의 종료 코드가 1이 되고, bash는 그 값을
# 스크립트 종료 코드로 덮어쓴다 — 성공한 실행이 실패로 보인다. 그래서 if로 감싼다.
cleanup() {
  if [ -n "$tmpdir" ]; then
    rm -rf "$tmpdir"
    tmpdir=""
  fi
}
trap cleanup EXIT

# 현재 작업 트리 기준 $1 디렉터리의 git 트리 해시를 TREE_RESULT에 담는다.
# 값을 반환(echo) 대신 전역에 담는 이유는, 명령 치환 $( )의 서브셸에서는 EXIT 트랩이
# 상속되지 않아 실패 시 임시 디렉터리가 남기 때문이다.
TREE_RESULT=""
compute_tree() {
  tmpdir=$(mktemp -d)
  # 빈 임시 인덱스에 -A로 add하면 .gitignore를 존중한 채 그 디렉터리 아래 전부가 담긴다.
  # 뒤집어 말하면 .gitignore에 걸리는 파일을 `git add -f`로 억지로 커밋해 두면, 여기서
  # 계산한 트리에는 그 파일이 빠져 HEAD의 트리와 영구히 어긋난다(원인 불명의 빨간불).
  # frontend/.gitignore의 Assets/ 패턴에 걸리는 것을 커밋해야 한다면 .gitignore부터 고쳐라.
  GIT_INDEX_FILE="$tmpdir/index" git add -A -- "$1"
  TREE_RESULT=$(GIT_INDEX_FILE="$tmpdir/index" git write-tree --prefix="$1/")
  cleanup
}

# 스탬프 파일에서 $1 키의 값을 뽑는다. 없거나 형식이 깨져 있으면 빈 문자열.
# .gitattributes가 이 파일을 eol=lf로 고정하지만 이중 방어로 CR을 걷어낸다.
# awk가 파일을 끝까지 읽고 끝내는 것도 의도한 것이다 — 첫 매치에서 exit하면 앞단 tr이
# SIGPIPE(141)로 죽고, pipefail 아래에서 그 값이 파이프라인 종료 코드가 되어 set -e에
# 걸린다. 진단 메시지 한 줄 없이 스크립트가 끝나는 실패라 애초에 만들지 않는다.
recorded_field() {
  tr -d '\r' < "$STAMP_FILE" | awk -v key="$1:" '
    !found && $1 == key && $2 ~ /^[0-9a-f]{40}$/ { value = $2; found = 1 }
    END { if (found) print value }
  '
}

# write 직전 경고: Assets/ 아래에 아직 git이 추적하지 않는 파일이 있으면 알린다.
# compute_tree는 미추적 파일도 트리에 담으므로(위 git add -A), 그대로 커밋하면
# 스탬프에는 있고 커밋에는 없는 상태가 되어 CI가 "재빌드하세요"로 잘못 진단한다.
warn_untracked() {
  local untracked
  untracked=$(git ls-files --others --exclude-standard -- "$SRC_DIR")
  if [ -n "$untracked" ]; then
    printf '경고: %s/ 에 아직 git이 추적하지 않는 파일이 있습니다. 스탬프에는 포함되므로 반드시 함께 커밋하세요:\n' "$SRC_DIR" >&2
    printf '%s\n' "$untracked" | sed 's/^/  /' >&2
  fi
}

write_stamp() {
  local force="${1:-}"
  local src_tree build_tree prev_build

  warn_untracked

  compute_tree "$BUILD_DIR"
  build_tree="$TREE_RESULT"

  # "재빌드 없이 도장만 다시 찍기" 차단. 이 검사가 없으면 check 실패 메시지를 읽은 사람이
  # write만 다시 돌리는 것이 가장 자연스러운 반응이 되고, 그 순간 이 검사 전체가 무의미해진다.
  # 진짜 재빌드였다면 Build/의 바이트가 바뀌므로 이 조건에 걸리지 않는다.
  if [ -z "$force" ] && [ -f "$STAMP_FILE" ]; then
    prev_build=$(recorded_field "$BUILD_KEY")
    if [ -n "$prev_build" ] && [ "$prev_build" = "$build_tree" ]; then
      err "지난 스탬프 이후 $BUILD_DIR/ 가 전혀 바뀌지 않았습니다. 재빌드 없이 스탬프만 갱신하면 어긋난 상태에 도장을 찍는 것이라 검사가 무의미해집니다. Unity WebGL 재빌드를 먼저 하세요."
      # 경로는 $0가 아니라 리터럴로 쓴다. $0는 호출한 디렉터리 기준이라 그 자리에서
      # 붙여넣으면 동작하지만, 이 스크립트의 다른 안내는 전부 레포 루트 기준이라
      # 한 메시지만 상대 경로가 튀어나오면 기준이 뒤섞인다.
      printf '  소스를 고쳤는데 번들 바이트가 정말 동일하게 나온 경우에만: scripts/frontend_build_stamp.sh write --force\n' >&2
      return 1
    fi
  fi

  compute_tree "$SRC_DIR"
  src_tree="$TREE_RESULT"

  cat > "$STAMP_FILE" <<STAMP
# 이 번들이 어느 소스에서 나왔는지 적어 둔 기록입니다 — 두 값 모두 git 트리 해시입니다.
# scripts/frontend_build_stamp.sh 가 씁니다. 손으로 고치지 마세요.
# 갱신 시점: Unity WebGL 재빌드 직후 (frontend/README.md의 재빌드 규칙 참고)
${SRC_KEY}: ${src_tree}
${BUILD_KEY}: ${build_tree}
STAMP
  printf '%s 갱신:\n  %s: %s\n  %s: %s\n' "$STAMP_FILE" "$SRC_KEY" "$src_tree" "$BUILD_KEY" "$build_tree"
}

# 기록된 값과 현재 트리를 대조한다. 다르면 진단을 찍고 1을 돌려준다.
compare_field() {
  local key="$1" dir="$2" recorded current

  recorded=$(recorded_field "$key")
  if [ -z "$recorded" ]; then
    err "$STAMP_FILE 에 '${key}: <40자리 해시>' 줄이 없습니다. Unity WebGL 재빌드 후 'scripts/frontend_build_stamp.sh write'로 다시 만드세요."
    return 1
  fi

  compute_tree "$dir"
  current="$TREE_RESULT"
  if [ "$recorded" = "$current" ]; then
    printf '  %-12s 일치 (%s)\n' "$key" "$current"
    return 0
  fi

  printf '  %-12s 불일치\n    기록된 해시: %s\n    현재 해시  : %s\n' "$key" "$recorded" "$current" >&2
  # 기록된 트리 오브젝트가 로컬에 있을 때만 차이를 보여 준다. CI 체크아웃은 얕아서
  # (fetch-depth: 2) 오래된 스탬프의 트리는 없을 수 있고, 그때 git diff는 실패한다.
  if git cat-file -e "${recorded}^{tree}" 2>/dev/null; then
    printf '    스탬프 이후 바뀐 파일:\n' >&2
    git diff --name-only "$recorded" "$current" | sed "s|^|      $dir/|" >&2
  fi
  return 1
}

check_stamp() {
  local failed=0

  if [ ! -f "$STAMP_FILE" ]; then
    err "$STAMP_FILE 이 없습니다. Unity WebGL 재빌드 후 'scripts/frontend_build_stamp.sh write'로 만드세요."
    return 1
  fi

  printf '%s 대조:\n' "$STAMP_FILE"
  compare_field "$SRC_KEY" "$SRC_DIR" || failed=1
  compare_field "$BUILD_KEY" "$BUILD_DIR" || failed=1

  if [ "$failed" -ne 0 ]; then
    err "커밋된 번들($BUILD_DIR/)과 소스($SRC_DIR/)가 마지막 재빌드 시점의 조합이 아닙니다. Unity WebGL 재빌드 후 'scripts/frontend_build_stamp.sh write'를 실행하고 $BUILD_DIR/ 와 $STAMP_FILE 을 함께 커밋하세요 (frontend/README.md의 재빌드 규칙 참고)."
    return 1
  fi
  return 0
}

print_trees() {
  compute_tree "$SRC_DIR"
  printf '%s: %s\n' "$SRC_KEY" "$TREE_RESULT"
  compute_tree "$BUILD_DIR"
  printf '%s: %s\n' "$BUILD_KEY" "$TREE_RESULT"
}

main() {
  cd "$(git rev-parse --show-toplevel)"
  case "${1:-}" in
    write)
      case "${2:-}" in
        "") write_stamp ;;
        --force) write_stamp force ;;
        *) usage; return 2 ;;
      esac
      ;;
    check) check_stamp ;;
    print) print_trees ;;
    *) usage; return 2 ;;
  esac
}

main "$@"
