# NFR-05 — NAT 도구 결과 마스킹 (#231)

관련 요구사항: F-17(비식별화). `docs/nfr-05-pii-masking.md`가 기술하는 backend 계층(#230)의
**보완 문서**다. 두 문서는 같은 요구사항의 서로 다른 적용 경로를 다룬다.

> 이 문서를 따로 둔 이유: `docs/nfr-05-pii-masking.md`는 진행 중인 다른 PR(#332)이 고치고
> 있어 같은 파일을 건드리면 충돌이 난다. #332가 머지된 뒤 이 문서의 요약을 그쪽 "미적용
> 경로" 절에 합치는 것이 후속 정리다.

## 위협 모델 — backend 마스킹으로 막히지 않는 이유

backend의 마스킹은 **backend가 조립한 프롬프트**를 가린다. 그런데 NAT 경로에서 backend가
보내는 것은 사용자 발화 하나뿐이고, 계좌 데이터는 그 다음 단계에서 NAT 프로세스 안에서
생긴다.

```
사용자: "내 잔고 어때?"
  1. backend --(user_msg only)--> NAT            # 마스킹해도 걸릴 PII가 없다
  2. NAT 라우터 --> trading_agent (ReAct)
  3. trading_agent --> KIS/mcp-trading 도구 호출
  4. 잔고 리포트(보유종목·수량·평단가·평가금액·예수금)가 Observation으로 컨텍스트에 삽입
  5. 그 컨텍스트가 openai_cloud_llm(api.openai.com)으로 전송   <-- 유출 지점
```

backend는 3~5단계를 보지 못한다. 실제로 나가는 문자열은
`mcp-trading/tests/fixtures/balance_report.json`의 `expected_text`와 동일한 형태다.

## 택한 선택지 — A(도구 결과 마스킹)

이슈 #231의 세 안 중 **A**를 택했다.

| 안 | 판단 |
| --- | --- |
| A. MCP 도구 결과 마스킹 | **채택.** 유출 지점(4단계)에 가장 가깝고, 방어 지점이 함수 3~4곳으로 좁다. 도구가 늘어도 한 곳에서 처리된다. |
| B. LLM 호출 직전 훅 | 미채택. NAT 1.6이 그 지점에 공개 훅을 주지 않아 `scripts/patch_vendor.py` 계열의 벤더 패치가 된다. `ci.yml`이 이미 벤더 패치 drift를 감시할 만큼 그 방식은 부채다(#152에서 같은 이유로 패치를 걷어냈다). |
| C. 계좌 취급 에이전트를 로컬 모델로 | 미채택(이번 범위에서). 외부 전송 자체가 사라지는 근본 해결이지만, 모델 품질·운영 비용 판단과 에이전트별 LLM 분리 검증이 필요해 한 PR에 담기지 않는다. A와 배타적이지 않다 — C를 나중에 택해도 A는 그대로 유효하다(Mem0·SQLite 저장 경로는 로컬 모델로 바꿔도 남는다). |

## 경계 — 어디서 가리고 어디서 되돌리는가

```
                          ┌─ 마스킹(들어오는 쪽) ─┐
KIS / mcp-trading / diary ─┤                      ├─> ReAct 컨텍스트 ─> OpenAI
                          └ _mask_account_tool_result

OpenAI ─> 최상위 워크플로 응답 ─ without_pii_placeholders ─> backend ─> 사용자
                    │
                    └ LLM이 만든 도구 인자 ─ restore_outbound ─> MCP / backend DB
```

- **마스킹 지점** — `finus_nat/src/nat_finus_nat/finus_api.py`의
  `_mask_account_tool_result()` 호출부. 항상 `_record_to_ledger()` **뒤**에 온다:
  원장(#152/#209)은 원문으로 판정해야 오류·빈 결과 리터럴 탐지가 마스킹에 흔들리지
  않는다. 원장에 남는 것은 불리언 3개뿐이라 원문 판정이 유출로 이어지지 않는다.
- **복원 지점 1(사용자)** — `agents.finus_reasoning_trace_agent`. 두 라우터 config의
  최상위 `workflow`이므로, 자리표시자가 이 경계를 넘어 사용자에게 나가는 경로는 없다.
  각주 부착(`with_reasoning_trace`, #294)이 본문 문자열 비교를 하므로 **복원은 각주
  부착 뒤**에 실행한다.
- **복원 지점 2(기계)** — `restore_outbound()`. LLM이 만든 MCP 인자와 매매일지 본문은
  실제 시스템에 나가는 값이라 원값이어야 한다. 여기서는 매핑에 없는 자리표시자를
  한국어 중립 문구로 바꾸지 않는다(주문 파라미터 자리에 "(이전 금액 1)"이 들어가는 것은
  토큰보다 나을 게 없고 실패 원인만 가린다).
- **중간 구간은 전부 마스킹 상태** — ReAct 컨텍스트, supervisor 히스토리, Mem0 저장,
  SQLite 대화 기록, api.openai.com.

세션 매핑은 요청 하나짜리 `ContextVar`(`pii.PII_SESSION`)에 담긴다. 최상위 워크플로가
한 번만 심고 안쪽은 읽기만 한다 — `DATA_TOOL_LEDGER`/`REASONING_TRACE`와 같은 이유로
`ContextVar.set()`은 안쪽에서 바깥으로 전파되지 않기 때문이다.

## 무엇을 가리고 무엇을 남기는가

가리면 분석이 불가능해지는 값까지 덮으면 기능이 죽는다. 그래서 **계좌 범위**만 가린다.

| 대상 | 마스킹 | 근거 |
| --- | --- | --- |
| `finus_mcp_trading_get_balance` / `_balance_rlz_pl` / `_today_orders` | O | mcp-trading은 서버 전체가 CANO 기반 계좌 TR 래퍼다 |
| `finus_save_diary` / `finus_list_diaries` | O | 사용자가 적은 자기 거래 기록 |
| `finus_account_balance`(KIS pass-through) | api_type별 | 한 도구가 계좌 TR과 시세 TR을 모두 태운다 |
| `finus_market_news` / `finus_disclosure_signal` / `finus_earnings_report` | X | 공개 정보. 가리면 분석만 망가지고 막히는 유출은 없다 |
| 종목명·수익률(%)·손익률 | X | F-17이 정의한 3종(계좌번호·원화 금액·보유 수량)이 아니다 |

KIS pass-through의 판정(`pii.kis_api_type_is_account_scoped`)은 **공개 시세 allowlist**
방식이다 — 목록에 없는 api_type은 전부 계좌 TR로 간주해 마스킹한다. deny-list로 두면
KIS가 새 계좌 TR을 추가하는 순간 조용히 평문으로 나간다. 반대 방향의 오판(시세를 가림)은
답변 품질만 떨어뜨린다. 비대칭이므로 모르는 쪽은 마스킹으로 눕힌다. 이 방향은
`_READONLY_API_ALLOWLIST_PREFIXES`(#66)와 같다.

## fail-closed

마스킹 엔진(`backend/pii_mask.py`)을 로드하지 못하거나 마스킹이 실패하면, 도구는 조회
결과 대신 `{"error": "pii_masking_unavailable", ...}`를 반환한다. 유출보다 조회 실패가
낫다는 것이 F-17의 전제다. 이 오류는 원장에 기록하지 않는다 — 도구는 실제로 성공했고,
성공을 실패로 뒤집으면 도구 강제 게이트(#152)가 같은 조회를 한 번 더 실행한다.

세션 상자가 없는 경로(최상위 워크플로를 거치지 않은 호출)에서도 마스킹은 적용된다.
복원할 방법이 없어 답변 품질은 떨어지지만, 상자가 없다고 원값을 흘리지는 않는다.

## 마스킹 엔진 재사용

`backend/pii_mask.py`를 **한 줄도 고치지 않고 파일 경로로 로드해 재사용**한다
(`pii._load_pii_mask()`). 정규식을 복사하면 두 계층의 판정이 조용히 갈라진다 —
실제로 #330·#333·#334가 그 정규식을 연달아 고쳤다.

finus_nat은 backend와 별도 패키지·별도 컨테이너라 `import backend.pii_mask`가 성립하지
않는다. `finus_nat/Dockerfile`이 그 파일 **하나만** 이미지에 복사하고
(`/workspace/backend/pii_mask.py`), `pii._pii_mask_candidates()`가
`FINUS_VENDOR_ROOT`(=`/workspace`) 기준으로 찾는다. 배치가 다르면
`FINUS_PII_MASK_PATH`로 파일 경로를 직접 지정한다.

## 알려진 한계

- **도구 호출마다 scope가 다르다.** `mask_pii`는 호출당 새 nonce를 뽑으므로, 같은 금액이
  두 도구 결과에 나와도 서로 다른 자리표시자가 된다. LLM은 두 값이 같다는 것을 알 수 없다.
- **이전 턴의 자리표시자는 복원되지 않는다.** SQLite 대화 기록에는 마스킹된 답변이
  저장되고 다음 턴 세션에는 그 scope가 없다. `unmask_pii`의 fail-open 경로가
  "(이전 금액 1)"로 바꾼다 — 조용한 오답 대신 관측 가능한 저하다. 저장을 원문으로
  되돌리면 다음 턴 히스토리가 평문으로 OpenAI에 재전송되므로 그쪽이 더 나쁘다.
- **절대 금액 기반 판단은 여전히 불가능하다.** `docs/nfr-05-pii-masking.md`가 기록한
  (a) 방식의 한계를 그대로 물려받는다. 부분 마스킹(#330)·조 단위 표기(#333)의 결론도
  같은 엔진을 쓰므로 그대로 적용된다.

## 이번 범위에서 뺀 것

- **Mem0 저장 경로.** `add_user_memory`는 LLM이 만든 텍스트를 받으므로, 도구 결과에서
  온 계좌 데이터는 이제 마스킹된 형태로 저장된다 — 즉 평문 축적은 줄어든다. 다만
  vendor `auto_memory_agent`가 무엇을 어떤 형태로 넣는지는 NAT 런타임을 띄워야 확인할
  수 있어 이번 PR에서 검증하지 못했다. Mem0 저장 내용 실측과 범위 확정은 후속이다.
- **`finus_order_verifier` 경로.** backend가 NAT의 검증 엔드포인트를 직접 부르며
  주문 수량·가격을 프롬프트에 싣는다. 라우터 워크플로 바깥이라 이 PR의 경계가 닿지
  않는다. 별도 이슈로 다뤄야 한다.
- **옵션 C(로컬 모델 라우팅).** 위 표 참고.
