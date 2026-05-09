# mcp-news 크롤링 제거 및 공식 API 전환 방안

## 목적

`mcp-news`가 Playwright로 네이버 검색/네이버증권 HTML을 직접 읽는 구조를 공식 API 기반으로 바꾼다. 목표는 무단 크롤링 리스크를 줄이면서 FastAPI 엔드포인트, 스케줄러, 분석 프롬프트 계약은 최대한 유지하는 것이다.

## 현재 구조

- `backend/main.py`
  - `/api/v1/news` -> `run_mcp_tool(NEWS_MCP_PARAMS, "get_market_news", {"stock_name": stock})`
  - `/api/v1/trading/trend` -> `run_mcp_tool(NEWS_MCP_PARAMS, "get_investor_trading", {"stock_name": stock})`
- `backend/scheduler.py`
  - `SIGNAL_SOURCES`가 `NEWS_MCP_PARAMS + get_market_news`를 호출한다.
  - 반환값은 단순 문자열 signal로 취급되므로 MCP 도구 내부 구현을 바꿔도 스케줄러 변경은 거의 필요 없다.
- `mcp-news/index.js`
  - `get_market_news`: 네이버 뉴스 검색 결과 페이지 크롤링.
  - `get_investor_trading`: 네이버 검색으로 종목코드 추출 후 네이버증권 외국인/기관 페이지 크롤링.
  - `get_research_reports`: 네이버증권 리서치 페이지 크롤링.
- `mcp-trading/index.js`
  - 이미 한국투자증권 API 토큰 발급, 토큰 캐시, 잔고 조회를 처리한다.

## 공식 API 근거

- 네이버 뉴스 검색 API
  - 공식 문서: <https://developers.naver.com/docs/serviceapi/search/news/news.md>
  - REST API이며 JSON endpoint는 `https://openapi.naver.com/v1/search/news.json`.
  - `query`, `display`, `start`, `sort` 파라미터를 지원한다.
  - `X-Naver-Client-Id`, `X-Naver-Client-Secret` 헤더가 필요하다.
  - 하루 호출 한도는 공식 문서 기준 25,000회.
  - 응답은 `title`, `originallink`, `link`, `description`, `pubDate` 중심이다. 기사 본문 전체를 주는 API는 아니다.
- 한국투자증권 Open API
  - 공식 포털: <https://apiportal.koreainvestment.com/>
  - 공식 샘플 저장소: <https://github.com/koreainvestment/open-trading-api>
  - `주식현재가 시세`: `/uapi/domestic-stock/v1/quotations/inquire-price`, TR ID `FHKST01010100`.
  - `주식현재가 투자자`: `/uapi/domestic-stock/v1/quotations/inquire-investor`, TR ID `FHKST01010900`.
  - 종목명 -> 종목코드 매핑은 공식 샘플의 `stocks_info` 종목코드 마스터파일 정제 흐름을 활용하는 쪽이 맞다.

## 권장 방향

### 1. `mcp-news`는 뉴스 API 전용 MCP로 축소

`get_market_news`의 이름과 반환 형식은 유지하고 내부만 네이버 뉴스 검색 API로 교체한다.

- 입력 유지: `{ stock_name: string }`
- 출력 유지: 줄바꿈으로 연결된 뉴스 문자열
- 검색어 예시: `${stock_name} 주식` 또는 `${stock_name} 실적`
- 정렬: 기본은 최신성 확보를 위해 `sort=date`
- 개수: 현재 UI/분석 흐름과 맞추려면 `display=3`
- 응답 정제:
  - `<b>` 태그 제거
  - HTML entity decode
  - `title`, `description`, `pubDate`, `link` 중 분석에 필요한 최소 정보만 문자열화

이 방식이면 `/api/v1/news`, 스케줄러 `SIGNAL_SOURCES`, `check_signal_significance`는 그대로 둘 수 있다.

### 2. 네이버증권 시세/수급 크롤링은 `mcp-trading`으로 이동

`get_investor_trading`은 시장/시세 데이터 성격이므로 `mcp-news`가 아니라 `mcp-trading`에 두는 것이 책임 경계상 맞다. 다만 백엔드 변경을 최소화하려면 두 단계로 진행한다.

1. `mcp-trading`에 신규 도구 추가
   - `resolve_stock_code`
   - `get_stock_quote`
   - `get_investor_trading`
2. `backend/main.py`의 `/api/v1/trading/trend`만 `NEWS_MCP_PARAMS`에서 `TRADING_MCP_PARAMS`로 바꾼다.

`/api/v1/trading/trend`의 HTTP 응답 구조는 그대로 유지한다.

```python
trend = await run_mcp_tool(
    TRADING_MCP_PARAMS,
    "get_investor_trading",
    {"stock_name": stock},
)
return {"status": "success", "data": {"stock": stock, "trend": trend}}
```

### 3. 종목명 검색은 공식 종목정보파일 기반 로컬 캐시로 처리

한국투자증권 시세 API는 종목코드를 요구한다. 현재는 네이버 검색 HTML에서 `code=005930` 패턴을 뽑는데, 이 부분을 없애야 한다.

권장 구현:

- 공식 `stocks_info` 흐름을 참고해 KRX/NXT 종목 마스터 파일을 주기적으로 갱신.
- `mcp-trading/data/stocks.json` 같은 로컬 캐시 생성.
- `resolve_stock_code("삼성전자") -> "005930"` 형태의 순수 조회 함수 제공.
- 동명이인/우선주/ETF 중복은 첫 구현에서 명확한 에러를 반환하고, 필요할 때 disambiguation을 추가한다.

이렇게 하면 메인 서비스는 계속 종목명을 넘기고 MCP 내부에서만 코드 변환을 수행할 수 있다.

## 구현안

### A안: 최소 변경안

- `mcp-news/index.js`
  - Playwright 제거.
  - `axios` 또는 Node `fetch`로 네이버 뉴스 검색 API 호출.
  - `get_market_news` 유지.
  - `get_investor_trading`, `get_research_reports`는 deprecated 처리하거나 에러 메시지로 공식 API 전환 필요를 안내.
- `mcp-trading/index.js`
  - `get_investor_trading` 신규 구현.
  - 기존 `get_balance`, 토큰 캐시, KIS env 설정 재사용.
- `backend/main.py`
  - `/api/v1/trading/trend`의 MCP params만 `TRADING_MCP_PARAMS`로 변경.
- `mcp-news/package.json`, `mcp-news/Dockerfile`
  - `playwright` 및 Chromium 설치 제거.
  - `mcp-news` 이미지 빌드 비용도 같이 줄어든다.

실현가능성: 높음.

### B안: 서비스 계층에 데이터 소스 어댑터 추가

백엔드에 `MarketDataProvider` 같은 추상화를 추가해 뉴스/시세 공급원을 감싼다. 장기적으로는 깔끔하지만 현재 목표인 “메인 서비스 로직 최소 변화”에는 과하다. 지금은 MCP 도구 계약을 유지하고 내부 구현만 바꾸는 A안이 더 적합하다.

실현가능성: 높지만 이번 변경 범위로는 비권장.

## 기능별 대체 가능성

| 현재 기능 | 현재 방식 | 대체 방식 | 가능성 | 비고 |
| --- | --- | --- | --- | --- |
| 최신 뉴스 3개 | 네이버 검색 HTML 크롤링 | 네이버 뉴스 검색 API | 높음 | 현재도 제목 중심이라 API 응답으로 충분함 |
| 종목코드 검색 | 네이버 검색 HTML에서 `code=` 추출 | KIS 종목정보파일 로컬 캐시 | 높음 | 초기 캐시 생성/갱신 스크립트 필요 |
| 현재가/등락률/거래량 | 네이버증권 HTML 일부 의존 가능 | KIS `inquire-price` | 높음 | TR ID `FHKST01010100` |
| 외국인/기관 수급 | 네이버증권 `frgn.naver` HTML | KIS `inquire-investor` | 중~높음 | 당일 데이터 제공 시점 등 차이 확인 필요 |
| 리서치 리포트 | 네이버증권 리서치 HTML | 미정 | 낮음 | 네이버 공식 검색 API나 KIS 기본 API로 동일 대체 어려움 |

## 리스크와 결정 필요 사항

- 네이버 뉴스 API는 기사 본문을 제공하지 않는다. 현재 `get_market_news`도 제목 중심이므로 영향은 작지만, 향후 본문 분석을 기대한다면 별도 뉴스 API 계약이 필요하다.
- 네이버 뉴스 API credential이 추가로 필요하다.
  - `NAVER_CLIENT_ID`
  - `NAVER_CLIENT_SECRET`
- 한국투자증권 API 호출량 제한을 고려해야 한다. 현재 `mcp-trading`은 토큰 캐시가 있으므로 토큰 발급 제한 리스크는 낮지만, 시세/투자자 조회 자체의 호출 제한은 별도 rate limit을 둬야 한다.
- `get_research_reports`는 공식 API 대체재가 불명확하다. 무단 크롤링 제거가 목적이면 제거 또는 비활성화가 맞다.
- 종목명 매칭은 한국어 별칭, 우선주, ETF/ETN 때문에 완전 자동화가 어렵다. 첫 단계는 정확히 일치하는 종목명과 6자리 코드 입력을 지원하고, 모호하면 에러를 내는 편이 안전하다.

## 단계별 작업 계획

1. 네이버 뉴스 API 설정 추가
   - `.env.example`: `NAVER_CLIENT_ID`, `NAVER_CLIENT_SECRET`
   - `mcp-news/index.js`: env 로딩 및 credential 검증
   - 검증: `get_market_news` MCP 호출이 뉴스 3개 문자열 반환

2. `mcp-news`에서 Playwright 제거
   - `package.json`, `package-lock.json`: `playwright` 제거, 필요 시 `axios` 추가
   - `Dockerfile`: Chromium 관련 apt 패키지와 `npx playwright install` 제거
   - 검증: `npm ci --omit=dev`, Docker build

3. `mcp-trading`에 시세/수급 도구 추가
   - `resolve_stock_code`
   - `get_stock_quote`
   - `get_investor_trading`
   - 검증: 삼성전자 기준 현재가/투자자 데이터 조회

4. `/api/v1/trading/trend` 호출 MCP 변경
   - `NEWS_MCP_PARAMS` -> `TRADING_MCP_PARAMS`
   - 응답 JSON 구조 유지
   - 검증: 기존 프론트/API 호출이 같은 형태로 동작

5. 스케줄러 영향 확인
   - `SIGNAL_SOURCES`는 계속 `get_market_news`만 사용하므로 변경 없음
   - 검증: `backend/tests/test_scheduler.py`, `backend/tests/test_services.py`

## 결론

실현 가능하다. 가장 안전한 경로는 `mcp-news`의 도구 이름과 문자열 반환 계약을 유지한 채 내부를 네이버 뉴스 검색 API로 바꾸고, 네이버증권에서 가져오던 시세/수급 로직은 `mcp-trading`에 한국투자증권 API 도구로 추가하는 것이다.

메인 서비스 변경은 `/api/v1/trading/trend`의 MCP 대상 변경 정도로 제한할 수 있다. 스케줄러와 분석 로직은 이미 외부 데이터를 문자열 signal로 취급하므로 거의 그대로 유지 가능하다.
