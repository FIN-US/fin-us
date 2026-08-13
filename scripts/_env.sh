_SCRIPTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

_fin_us_resolve_integrate_root() {
  local d="${_SCRIPTS_DIR}"
  while true; do
    if [[ -f "${d}/docker-compose.yml" ]]; then
      if [[ -d "${d}/fin-us/backend" ]] || [[ -d "${d}/backend" ]]; then
        printf '%s\n' "$(cd "${d}" && pwd)"
        return 0
      fi
    fi
    [[ "${d}" == "/" ]] && return 1
    d="$(cd "${d}/.." && pwd)"
  done
}

if [[ -n "${FIN_US_INTEGRATE_ROOT:-}" ]] && [[ -d "${FIN_US_INTEGRATE_ROOT}" ]]; then
  FIN_US_INTEGRATE_ROOT="$(cd "${FIN_US_INTEGRATE_ROOT}" && pwd)"
else
  FIN_US_INTEGRATE_ROOT="$(_fin_us_resolve_integrate_root)" || {
    echo "ERROR: Fin-Us integrate root not found (need docker-compose.yml and backend/ or fin-us/backend/)." >&2
    echo "       Set FIN_US_INTEGRATE_ROOT to the directory that contains docker-compose.yml." >&2
    return 1 2>/dev/null || exit 1
  }
fi
export FIN_US_INTEGRATE_ROOT

if [[ -d "${FIN_US_INTEGRATE_ROOT}/fin-us" ]]; then
  export FIN_US_DIR="${FIN_US_INTEGRATE_ROOT}/fin-us"
else
  export FIN_US_DIR="${FIN_US_INTEGRATE_ROOT}"
fi
export FINUS_NAT_DIR="${FIN_US_INTEGRATE_ROOT}/finus_nat"
export FIN_BACKEND="${FIN_US_DIR}/backend"
# Unity WebGL 프로젝트. 배포되는 산출물은 이 아래 Build/ 이며 compose의 frontend(nginx)가 서빙한다.
export FIN_FRONTEND="${FIN_US_DIR}/frontend"
