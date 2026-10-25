# scripts

설치·실행·점검 셸 스크립트. 저장소 루트에서 `bash scripts/<이름>`으로 실행.

## 사용자용

| 스크립트 | 역할 |
| :--- | :--- |
| `setup_env.sh` | 대화형으로 루트 `.env` 생성 (기능별로 쓸지 묻고 API 키 입력) |
| `check_env.sh` | Docker·`.env` 필수 값 점검 |
| `setup_deps.sh` | MCP 서버 npm 의존성 설치 + Docker 이미지 빌드 |
| `run_stack.sh` | 전체 스택 실행 (`docker compose up`). 서비스 이름을 주면 그 서비스만 |
| `reset_clean.sh` | 컨테이너·이미지·설치된 의존성 삭제 (`.env`와 데이터는 유지). `-y`로 확인 생략 |

## 개발·내부용

| 스크립트 | 역할 |
| :--- | :--- |
| `install_fin_us_mcp.sh` | `mcp/` 세 서버에 `npm ci` |
| `frontend_build_stamp.sh` | Unity 번들과 소스의 짝을 기록(`write`)·검사(`check`). 사용법은 [frontend/README.md](../frontend/README.md) |
| `finus-nat-entrypoint.sh` | `finus-nat` 컨테이너 진입점 (MCP 의존성 확인, 메모리 설정에 맞는 config 선택) |
| `_env.sh`, `_compose_up.sh` | 다른 스크립트가 불러 쓰는 공용 함수 |
| `demo_rehearsal/` | 시연 영상 리허설. 채팅 장면을 실제 사용자 경로로 N회 돌려 장면별 통과율을 잰다(Python, 주문 가드 포함). 사용법은 [demo_rehearsal/README.md](demo_rehearsal/README.md) |
