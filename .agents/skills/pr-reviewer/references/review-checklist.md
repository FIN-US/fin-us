# PR 리뷰 체크리스트 (fin-us 전용)

**CI가 이미 잡는 것은 여기 없습니다.** `.github/workflows/ci.yml`이 backend·finus_nat pytest,
mcp-trading·mcp-dart npm test, Unity `Assets`/`Build` 드리프트, hadolint, `nginx -t`를 돌립니다.
빨간불이 나면 그건 리뷰할 일이 아니라 고칠 일입니다. 이 목록은 **CI가 못 잡는 것**만 담습니다.

## 돈이 나가는 경로 (`backend/trading_orders.py`, `mcp-trading/`)

- [ ] 부수효과(주문 저장·체결)를 확정한 뒤의 실패가 재시도에서 **같은 결과로 수렴**하는가
- [ ] 중복 체결 방지가 살아 있는가 (`mcp-trading/order-dedup.js`의 `OrderDedupStore`)
- [ ] 종목코드 검증 가드를 우회하는 경로가 생기지 않았는가 (`backend/stock_code.py`)
- [ ] 새 주문 경로에 사용자 확인 단계가 있는가

## Redis 상태 (`backend/redis_state.py`)

- [ ] 실패 시 상태가 유실되면 사용자가 무엇을 보게 되는가 — 조용한 유실인가 로그가 남는가
- [ ] 새 필드에 기본값이 있어 **구버전 레코드가 역직렬화되는가** (배포 시 남아 있는 레코드)
- [ ] 소켓 타임아웃 없이 무한 대기할 수 있는 호출이 추가되지 않았는가
- [ ] 실 redis 없이 도는 테스트가 실제 동작을 검증한다고 착각하게 만들지 않는가

## 텔레그램 (`backend/telegram_commands.py`, `telegram_notifier.py`)

- [ ] 새 `except Exception`이 `TelegramSendError` 재전파를 가로막지 않는가
      (구조 불변식 테스트가 강제하지만, allowlist 추가는 리뷰 대상이다)
- [ ] 재시도에 벽시계 상한이 있는가 — 백오프 합만 계산하고 HTTP 타임아웃을 빠뜨리지 않았는가
- [ ] 로그·에러 메시지에 봇 토큰이나 사용자 식별자가 실리지 않는가

## 외부로 나가는 데이터

- [ ] 외부 LLM 호출 직전 PII 마스킹 계층을 우회하는 경로가 아닌가
- [ ] 새 API 엔드포인트에 인증·Origin 검사·레이트리밋 중 필요한 것이 붙었는가
      (`/api/v1/ws`, `/api/v1/analyze`가 이 문제를 겪었다)
- [ ] 시크릿이 `.env.example`에는 이름만, 실제 값은 `.env`에만 있는가

## 의존성·빌드

- [ ] `finus_nat` 의존성을 바꿨다면 `scripts/patch_vendor.py`의 **네 대상**에 패치가 여전히 붙는가
- [ ] `mcp-news`를 고쳤다면 테스트를 **직접** 돌렸는가 (CI에 mcp-news 잡이 없다)
- [ ] `frontend/Assets/`를 고쳤다면 `frontend/Build/` 재빌드가 동반됐는가

## 테스트

- [ ] 새 가드가 **제거됐을 때 실제로 빨간불이 되는가** — 통과만 확인한 테스트는 공허할 수 있다
- [ ] 엣지케이스: 취소·재시작·부분 실패 이후의 재실행 경로
