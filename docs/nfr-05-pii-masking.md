# NFR-05 — 외부 LLM 호출 시 개인·계좌 정보 비식별화

관련 요구사항: F-17(비식별화). 이 문서는 F-17/NFR-05 관련 작업이 진행되며 갱신된다.
레포 전체를 조사했으나 이 요구사항을 기록한 기존 문서를 찾지 못해(#230 착수 시 확인)
이 파일을 새로 만들어 갱신한다.

## 범위

외부 LLM 제공자(OpenAI/Anthropic/Ollama/NAT)로 나가는 프롬프트에서 다음 3종을 마스킹한다.

| 대상 | 비고 |
| --- | --- |
| 한국 계좌번호 | `KIS_ACCOUNT_NO` 형식: CANO(8자리) + 상품코드(2자리), 하이픈 유무 무관 |
| 원화 금액 | `1,234,567원` / `1234567원` / `123만원` / (라벨 컨텍스트에서) `1234567` |
| 보유 수량 | 잔고 리포트의 `hldg_qty` 유래 값(`N주`) |

## 적용 경로 — 해소됨 (#230)

`backend/services.py`의 `llm_chat()`이 OpenAI/Anthropic/Ollama/NAT 4개 provider 경로의
단일 진입점이다. `backend/pii_mask.py`의 `mask_pii()`/`unmask_pii()`를 이 함수 안에서
호출해, `llm_chat()`을 거치는 모든 프롬프트가 자동으로 마스킹된다.

- `perform_stock_analysis`의 `_build_nat_prompt`/`_build_toolless_prompt` 경로
- `generate_morning_briefing`이 조립하는 모닝 브리핑 프롬프트 — KIS 잔고 리포트
  텍스트(`get_balance` 결과)를 backend가 직접 프롬프트에 넣는 유일한 경로이며,
  이 계층 신설 전에는 계좌 평가금액·수익률·보유수량이 마스킹 없이 나갔다
  (`mcp-trading/tests/fixtures/balance_report.json` 픽스처로 실측·회귀 테스트함)
- `check_signal_significance`(뉴스/시그널 유의미성 판정) 경로

새 호출 경로가 provider별 구현(`_llm_openai_chat` 등)을 `llm_chat()` 밖에서 직접 부르면
`backend/tests/test_services.py::test_no_bypass_of_llm_chat_masking_layer`가 실패한다
(AST로 `backend/services.py`를 스캔해 호출부가 `llm_chat` 함수 본문 밖에 있으면 잡는다).

## 방식 — (a) 자리표시자 + 매핑 복원

```
보내는 프롬프트: "<ACCOUNT_1> 계좌, 삼성전자 <QTY_1>주, 평가금액 <AMOUNT_1>, 총자산 <AMOUNT_2>"
응답 수신 후   : <AMOUNT_1> -> 12,345,000원 로 역치환
```

매핑은 `llm_chat()` 호출마다 새로 만들어지는 지역 변수다. 모듈 전역에 두면
`asyncio.gather`로 병렬 호출되는 여러 `llm_chat()` 호출이 서로의 매핑을 덮어쓸 수
있어(스케줄러가 이런 구조로 호출한다), 요청 단위 격리를 지역 변수로 강제한다.

**한계**: 상대 비교(`AMOUNT_2 > AMOUNT_1`, 즉 "총자산이 평가금액보다 큰가")는 자리표시자만으로도
LLM이 판단할 수 있어 유지되지만, **절대 금액 기반 판단**("이 종목 비중이 1천만원으로
과도하다")은 원값이 프롬프트에 없으므로 LLM이 할 수 없다. 정밀한 절대 판단이 필요해지면
(b) 비율·구간 변환(예: "포트폴리오 대비 12%")을 별도 이슈로 검토해야 한다 — 변환 규칙·구간
경계 설계 비용이 #230 범위를 넘어 이번에는 채택하지 않았다.

**역치환 fail-open**: LLM이 자리표시자를 변형하거나(`<AMOUNT_1>` → `<AMOUNT1>`) 존재하지
않는 자리표시자를 지어내면(`<AMOUNT_9>`) `unmask_pii()`는 예외를 던지지 않고 해당 부분을
원문(자리표시자) 그대로 남긴 채 경고 로그만 남긴다. 역치환 실패가 분석 결과 저장을 막지
않는다 — `_resolve_stock_code`의 실패 폴백, `stock_code.py`의 마스터 로드 fail-open과
같은 원칙이다.

## 정규식 채택 근거 (Presidio 미도입)

착수 시 실제 프롬프트 샘플(`_build_toolless_prompt`, `_build_nat_prompt`,
`generate_morning_briefing`이 조립하는 프롬프트, `mcp-trading/tests/fixtures/balance_report.json`
공유 픽스처)을 확인한 결과:

- 대상 3종은 모두 구조화된 표기 패턴이라 정규식으로 충분히 식별된다.
- **자유 입력 경로는 실재한다.** 다음 두 경로는 backend가 조립하지 않은 원문을 그대로
  LLM에 전달한다.
  - `backend/telegram_commands.py:1349-1356`의 `_handle_chat_fallback` — 명령어에
    매칭되지 않은 텔레그램 사용자 원문 텍스트를 그대로
    `llm_runner("nat", text, conversation_id=f"telegram:{chat_id}")`로 보낸다.
  - `backend/services.py`의 `check_signal_significance` — 외부 뉴스/시그널 원문
    (약 1000자)을 프롬프트에 삽입한다.
  - 이 경로들에는 인명·전화번호·주민등록번호·이메일이 들어올 수 있고, 현재
    recognizer 3종(계좌/금액/수량)은 이를 잡지 못한다. 다만 F-17이 정의한 마스킹
    대상은 이 3종뿐이며, 인명·연락처 등 NER이 필요한 대상은 이번 범위 밖이다
    (아래 "미적용 경로" 참고).
- `presidio-analyzer`는 spaCy NER 모델 다운로드가 Docker 이미지·CI 시간에 얹히는 비용이
  있어, 이 범위(계좌/금액/수량 3종)에서는 이점 없이 비용만 커진다.

근거는 이슈 #230 코멘트에도 남긴다.

## 미적용 경로 (한계로 명시)

이 계층은 **`llm_chat()`을 거치는 프롬프트만** 막는다. 아래 경로·대상은 이 계층 밖이며
후속 이슈에서 별도로 다루거나(1, 2, 4) 구조적 한계·의도된 트레이드오프로 남긴다(3, 5).

1. **NAT 내부 도구 호출 경로** — NAT 멀티에이전트가 자체적으로 MCP 도구(KIS 잔고 등)를
   호출해 만드는 프롬프트 조각은 backend가 보지 못하므로 마스킹할 수 없다. backend는
   `user_msg`만 NAT에 보내고, NAT 프로세스 내부에서 추가로 조회한 데이터는 그 안에서
   바로 OpenAI로 나간다. → **#231**에서 다룬다.
2. **Telegram 경유 잔고 조회(`/balance`, 주문 확인 메시지)** — LLM 호출이 아니라 이
   미들웨어의 사정거리 밖이다. `/balance`는 사용 목적 자체가 원값 표시이므로 마스킹을
   적용하면 명령의 존재 이유가 사라진다. → **#232**에서 옵션 결정과 근거 기록을 다룬다.
3. **정규식 커버리지의 구조적 한계** — `_ACCOUNT_RE`는 KIS_ACCOUNT_NO 형식(10자리,
   하이픈 유무 무관)만 다룬다. 다른 증권사 형식이나 표기 편차는 놓칠 수 있다.
4. **NER 대상(인명·연락처 등)** — "정규식 채택 근거" 절에서 확인했듯 자유 입력 경로
   (`_handle_chat_fallback`, `check_signal_significance`)가 실재하므로, 이 항목은
   "자유 입력이 없어서 불필요하다"가 아니라 **이번 범위(F-17의 계좌/금액/수량 3종) 밖이라
   다루지 않았다**는 뜻이다. 인명·전화번호·주민등록번호·이메일 마스킹이 필요해지면
   Presidio 등 NER 도입을 별도 이슈에서 검토해야 하는 열린 항목으로 남긴다.
5. **의도된 과탐(false positive)** — 라벨 없는 순수 10자리 숫자는 원리적으로 KIS
   계좌번호와 구분할 수 없다. 실측으로 재현된 예:

   ```
   "유닉스 타임스탬프 1723526400 기록" → "유닉스 타임스탬프 <ACCOUNT_1> 기록"
   "사업자등록번호 1234567890 확인"     → "사업자등록번호 <ACCOUNT_1> 확인"
   ```

   이 방향의 오류는 유출이 아니라 "LLM에 틀린 타입 문맥을 준다"는 품질 문제이고,
   반대 방향(과소탐 = 유출)보다 안전한 쪽이라 의도적으로 감수한다. (a) 방식은 왕복
   무손실이므로 역치환 후 사용자에게 보이는 값은 정확하다 — 과탐은 LLM이 보는
   프롬프트에만 영향을 주고, 최종 출력의 원값 표시를 훼손하지 않는다.

## NAT 경로에도 마스킹을 적용하는 이유

리뷰에서 "NAT은 자체 MCP로 잔고를 재조회해 OpenAI로 보내므로(#231) NAT 경로 마스킹은
유출을 막지 못하면서 NAT 컨텍스트만 훼손하는 게 아닌가"라는 지적이 있었다. NAT이
사실상 기본 provider라 영향이 크다고 판단해 별도로 조사했고, 결론은 다음과 같다.

**결정: NAT 경로에도 마스킹을 유지한다(심층 방어). 코드 변경 없음.**

근거:

- `_llm_nat_chat`(`backend/services.py:636-654`)은 NAT 서버로
  `messages=[{"role": "user", "content": user_msg}]` 1건만 보낸다. 이 `user_msg`에
  잔고 원본이 들어가는 경로는 `generate_morning_briefing`이 `_collect_morning_context`의
  `balance_text`를 직접 조립해 넣는 경우가 유일하다 — 그리고 그건 이 마스킹 계층의
  **의도된 적용 대상**이지 "훼손"이 아니다.
- 리뷰어가 우려한 손실(잔고 컨텍스트 상실)은 실제로는 NAT **서브에이전트가 자체
  조회한** 잔고에서 발생하는데, 그 경로는 `user_msg`와 무관하게 NAT 프로세스 내부에서
  OpenAI로 나간다(`finus_nat/configs/agents/trading_agent.yml:12-16`의
  `kis-trading-mcp-tool` → `trading_openai_llm`). 즉 backend의 마스킹을 끄든 켜든 그
  경로는 달라지지 않는다 — 여기서 마스킹을 빼도 얻는 것이 없다.
- 반면 NAT 분기만 마스킹에서 제외하면 "`llm_chat()`을 거치는 모든 provider가 동일하게
  마스킹된다"는 불변식이 깨진다. 이 불변식은
  `backend/tests/test_services.py::test_no_bypass_of_llm_chat_masking_layer`가 AST로
  지키고 있고, provider별 특례가 생기면 향후 provider 추가 시 마스킹이 조용히 빠질
  위험이 커진다.
- 감수하는 비용: 모닝 브리핑에서 잔고 금액이 자리표시자로 나가므로 절대 금액 기반
  판단("이 종목 비중이 1천만원으로 과도하다")은 NAT이 할 수 없다. 이는 "방식" 절에
  이미 적힌 (a) 방식의 알려진 한계와 같은 것이며, 이를 감수한다.

정리하면 "미적용 경로" 1번 항목(#231, NAT 내부 도구 호출 경로)과는 상충하지 않는다.
NAT 내부에서 서브에이전트가 자체 조회해 만드는 프롬프트 조각은 여전히 #231의 몫이고,
backend가 `user_msg`로 직접 조립해 넣는 잔고 텍스트에 대한 마스킹은 그와 별개로
유지한다.

## 구현

- `backend/pii_mask.py` — recognizer 3종, `mask_pii()`/`unmask_pii()`. services.py에
  인라인하지 않고 별도 모듈로 분리했다(#140에서 `stock_code.py`로 공용화한 선례를 따름).
- `backend/services.py:llm_chat()` — 통합 지점.
- 테스트: `backend/tests/test_pii_mask.py`(recognizer별 단위 테스트, 왕복 무손실,
  실제 KIS 잔고 리포트 픽스처 기반 테스트, fail-open 테스트),
  `backend/tests/test_services.py`(마스킹 전 발신 차단, 역치환, 동시 요청 격리,
  우회 경로 회귀 가드).
