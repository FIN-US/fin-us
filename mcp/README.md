# mcp

외부 데이터 API를 감싸는 MCP(Model Context Protocol) 서버 3개 (Node.js). 별도 컨테이너로 뜨지 않고, backend와 agents가 필요할 때 stdio 자식 프로세스로 실행.

## 서버와 도구

| 서버 | 데이터 출처 | 도구 |
| :--- | :--- | :--- |
| `trading/` | 한국투자증권 Open API | `get_balance`, `get_balance_rlz_pl`, `get_orderable_cash`, `get_stock_quote`, `get_investor_trading`, `get_today_daily_orders`, `resolve_stock_code`, `place_order` |
| `news/` | 네이버 검색 API | `get_market_news` |
| `dart/` | OpenDART | `get_disclosure_signal`, `get_earnings_report` |

- API 키는 루트 `.env`에서 읽음 (`KIS_*`, `NAVER_*`, `DART_API_KEY`).
- `place_order`는 backend의 텔레그램 주문 경로에서만 호출. 에이전트에는 연결되지 않음.

## trading/ 구성

```
trading/
├── index.js            MCP 서버 진입점·도구 등록
├── kis-client.js       KIS 주문 POST (해시키 포함)
├── order.js, order-submit.js, order-dedup.js  주문 본문 생성·제출·중복 주문 방지 원장
├── balance.js, balance-rlz-pl-report.js, orderable-cash.js  잔고·실현손익·주문가능금액
├── token-cache.js      KIS 접근 토큰 파일 캐시
├── kis-rate-limit.js   유량 제한 판정·요청 로그
├── stock-master.js     종목명 → 종목코드 해석
├── data/stocks.json    종목 마스터 (코스피·코스닥)
└── scripts/update_stock_master.py  종목 마스터 갱신
```

- 신규 상장 종목이 인식되지 않으면 `python3 mcp/trading/scripts/update_stock_master.py`로 마스터 갱신.
- 실계좌 주문은 `.env`의 `KIS_ORDER_ENV=real`과 `KIS_REAL_ORDER_ENABLED=true`가 모두 있어야 실행.

## 개발

```bash
bash scripts/install_fin_us_mcp.sh      # 세 서버 의존성 설치 (npm ci)
(cd mcp/trading && npm test)            # 서버별 테스트 (news, dart도 동일)
```
