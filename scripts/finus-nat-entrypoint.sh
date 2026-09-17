#!/bin/sh
set -e

for pkg in mcp-news mcp-trading mcp-dart; do
  modules="/workspace/${pkg}/node_modules"
  if [ ! -d "${modules}" ] || [ -z "$(ls -A "${modules}" 2>/dev/null)" ]; then
    echo "finus-nat: installing ${pkg} npm dependencies..."
    (cd "/workspace/${pkg}" && npm ci --omit=dev)
  fi
done

# 메모리 플래그 (#397) — finus_nat/scripts/run.sh와 같은 규칙. FINUS_NAT_CONFIG_FILE을 직접 주면 그대로 쓴다.
# 모르는 값은 켜짐으로 넘기지 않고 멈춘다(오타가 조용히 사용자 선호 저장을 켜지 않게).
if [ -z "${FINUS_NAT_CONFIG_FILE:-}" ]; then
  case "$(printf '%s' "${FINUS_MEM0_ENABLED:-1}" | tr '[:upper:]' '[:lower:]')" in
    1|true|yes|on) FINUS_NAT_CONFIG_FILE="configs/router.yml" ;;
    0|false|no|off) FINUS_NAT_CONFIG_FILE="configs/router_nomemory.yml" ;;
    *)
      echo "finus-nat: FINUS_MEM0_ENABLED 값을 해석할 수 없습니다: '${FINUS_MEM0_ENABLED}' (1/0, true/false, yes/no, on/off)" >&2
      exit 1
      ;;
  esac
  export FINUS_NAT_CONFIG_FILE
fi

exec "$@"
