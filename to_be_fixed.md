# To Be Fixed

Issue #32 1차 구현 이후 남은 한계와 후속 개선 항목입니다.

## 1. 종목 마스터 범위 확대

현재 `mcp-trading/data/stocks.json`은 초기 검증용 소수 종목만 포함합니다.

- 영향: 삼성전자, SK하이닉스, 현대차, NAVER, 카카오 외 종목은 종목명 조회가 실패할 수 있음
- 임시 우회: 6자리 종목코드를 직접 입력
- 개선 방향: KIS 종목 마스터 또는 공식 배포 파일 기반으로 `stocks.json` 자동 갱신 스크립트 추가

## 2. 리서치 리포트 기능 대체

`get_research_reports`는 무단 크롤링 제거 방침에 따라 deprecated 오류를 반환합니다.

- 영향: NAT 분석이 리서치 리포트 없이 뉴스와 수급 중심으로 수행됨
- 개선 방향: 공식/계약 가능한 리서치 데이터 공급원을 정한 뒤 별도 MCP 도구로 재구현

## 3. KIS 환경 구분 명시화

현재 KIS 실전/모의 구분은 `KIS_URL` 값에 의존합니다.

- 영향: URL과 TR ID 조합이 맞지 않으면 403 또는 KIS 오류가 발생할 수 있음
- 개선 방향: `KIS_ENV=real|virtual` 같은 명시 설정을 추가하고, 환경별 URL/TR ID를 코드에서 선택

## 4. KIS 토큰 캐시 안정화

현재 KIS access token은 stdio MCP 프로세스 재시작 문제를 피하기 위해 `/tmp/finus-kis-token-*.json` 파일에 캐시합니다.

- 영향: 컨테이너 재생성 또는 `/tmp` 초기화 시 토큰 캐시가 사라짐
- 개선 방향: Redis, DB, 또는 backend 서비스 레벨 캐시로 이전
- 추가 고려: 토큰 파일 권한, 만료 처리, 다중 컨테이너 환경에서의 공유 방식

## 5. MCP 에러 응답 구조화

현재 MCP 도구 실패는 HTTP 200 응답 안의 문자열로 전달됩니다.

예:

```json
{
  "trend": "get_investor_trading 실행 중 에러 발생: ..."
}
```

- 영향: 프론트엔드와 호출자가 성공/실패를 안정적으로 구분하기 어려움
- 개선 방향: MCP 결과를 `{ "ok": false, "error_code": "...", "message": "..." }` 같은 구조로 통일하고 backend에서 HTTP 상태 코드 매핑

## 6. NAT 경로와 backend 직접 호출 경로 중복

현재 시장 데이터 호출 경로가 두 개입니다.

- `/api/v1/news`, `/api/v1/trading/trend`: backend가 MCP 직접 호출
- `/api/v1/analyze?provider=nat`: finus-nat가 MCP 직접 호출

- 영향: 환경 변수 전달, 토큰 캐시, 오류 처리 로직이 중복됨
- 개선 방향: 시장 데이터 조회를 한 계층으로 모으고 NAT는 그 결과를 입력으로 받도록 정리

## 7. NAT 분석 결과 품질 보강

NAT 분석은 LLM이 도구 결과를 읽고 JSON을 생성합니다.

- 영향: `trading_trend`가 실제 KIS 수급이 아닌 뉴스성 요약으로 채워질 수 있음
- 개선 방향: backend가 뉴스/수급 원문을 구조화해 조립하고, LLM은 판단과 요약만 담당하도록 역할 분리

## 8. KIS 응답 필드 검증 확대

삼성전자 기준 수급 조회는 확인했지만, 다양한 종목과 장 상황에 대한 검증은 아직 부족합니다.

- 영향: 일부 종목, 휴장일, 장중/장후, KIS 오류 코드에 따라 출력 품질이 떨어질 수 있음
- 개선 방향: KIS 응답 샘플별 fixture를 추가하고 포맷터 단위 테스트 확대

## 9. Docker 빌드 최적화

Playwright 제거로 빌드 부담은 줄었지만 backend와 finus-nat 이미지가 각각 Node/Python 의존성을 설치합니다.

- 영향: 변경 범위가 작아도 이미지 재빌드 시간이 길 수 있음
- 개선 방향: 공통 MCP 베이스 이미지, dependency layer 분리, 개발용 volume mount 전략 검토
