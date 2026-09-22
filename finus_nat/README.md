# Fin-Us NAT 설정

기본 config는 configs/router.yml(사용자 메모리 켜짐)이고, `FINUS_MEM0_ENABLED=0`이면 configs/router_nomemory.yml을 씁니다(#397). run.sh와 Docker 이미지(엔트리포인트)가 같은 규칙으로 고릅니다.

finus_nat/scripts/run.sh 으로 실행하여 cli 환경에서 에이전트를 구동해볼 수 있습니다.
--memory 옵션을 사용하면 finus_nat/configs/router.yml을 사용합니다(기본값).
--nomemory 옵션을 사용하면 finus_nat/configs/router_nomemory.yml을 사용합니다.
--once 옵션을 사용하면 사용자 쿼리를 한번만 실행하고 워크플로우를 종료합니다.

사용 예시) bash finus_nat/scripts/run.sh --nomemory

## SQLite 대화 히스토리

router.yml과 router_nomemory.yml은 기본적으로 최근 대화 히스토리를 SQLite에 저장하고 다음 요청에 다시 주입합니다. NAT는 HTTP `conversation-id` 헤더로 세션을 구분합니다.
이는 OpenAI compatible /v1/chat/completion을 사용하기 위함입니다.

## 추론 각주 (routed_agent / tools_used)

응답에 담당 에이전트와 실제 실행된 도구 목록을 실어 보내, 텔레그램 봇이 답변 하단에
근거 각주를 렌더링합니다 (#260).

`tools_used`의 각 항목은 `{"name": str, "ok": bool, "empty": bool}`입니다. 도구 강제
원장이 구분하는 세 상태를 그대로 넘깁니다 — 오류(`ok=false`), 성공했지만 0행
(`ok=true, empty=true`, #209), 데이터 있음(`ok=true, empty=false`). 상태를 빼고 이름만
넘기면 소비자가 실패·빈 결과까지 "확인한 자료"로 표시해, 답변이 근거하지 않은 데이터를
근거로 제시하게 됩니다. 표기는 소비자가 정합니다(backend는 `(실패)`/`(결과 없음)`).

**두 라우터 모두 동작합니다.** 두 config의 최상위 `workflow`는
`finus_reasoning_trace_agent`이고, 각주는 오직 여기서만 붙습니다(#273). 그 아래에
무엇이 오든 — #397 이전 router.yml처럼 vendor `auto_memory_agent`가 끼든, 지금처럼
바로 `finus_sqlite_transcript_agent`가 오든 — 부착 지점은 같습니다.

**단일 문자열 입력은 각주 없이 평문만 돌려줍니다.** `nat run --input ...`처럼 단일
문자열로 워크플로를 부르면 각주를 실을 응답 객체가 없으므로 본문 텍스트만 반환합니다.
종전 `finus_sqlite_transcript_agent`는 이 경우에도 각주가 실린 `ChatResponse`를
돌려줬으니 **동작이 바뀐 지점입니다**(#273).

`run.sh`에서 이 경로를 타는 것은 **`--once`뿐입니다.** 기본 모드는 채팅 REPL(`nat serve`
+ `finus-chat`)이고, REPL은 HTTP로 `messages`를 보내므로 각주가 그대로 나옵니다.
backend·scheduler도 마찬가지로 영향받지 않습니다. 즉 각주가 안 보이는 경우는
`run.sh --once`와 `nat run --input` 두 가지뿐입니다.

이 지점을 vendor 바깥으로 올린 이유: `auto_memory_agent`의 `_response_fn` 시그니처가
`(input_message: str) -> str`이라, 안쪽에서 `ChatResponse`에 붙인 두 필드가 그 래퍼를
통과하면서 버려집니다. backend는 필드가 없으면 각주를 조용히 생략하므로, 예전
router.yml에서는 경고도 예외도 없이 각주만 사라졌습니다(#273). vendor를 감싸는 대신
부착 지점을 올렸으므로 NAT 업그레이드로 vendor 시그니처가 바뀌어도 깨지지 않습니다.

**실패한 응답에는 붙지 않습니다.** vendor `auto_memory_agent`는 예외를 삼키고 그
오류 문자열을 답변으로 돌려줍니다(router.yml이 `verbose: true`). 이때 추론 기록은
남아 있으므로, 기록의 유무만 보고 붙이면 오류 문자열 아래에 "담당: 뉴스 에이전트"가
정상 답변과 똑같은 모습으로 달립니다 — 답변이 근거하지 않은 경로를 근거로 제시하는
셈입니다(#294). 그래서 supervisor가 브랜치의 답변 본문을 기록에 함께 남기고, 최상위는
돌려보내는 본문이 **그 본문과 같을 때만** 두 필드를 싣습니다. 판정 기준이 "브랜치가
성공했는가"가 아닌 이유는 브랜치가 성공한 뒤 메모리 쓰기(`capture_ai_response`)가
터지는 변종 때문입니다 — 그때는 기록이 가득 찬 채 본문만 오류 문자열입니다. 생략할
때는 경고 로그를 남깁니다(#273처럼 조용히 사라지지 않게).

두 필드가 빠지면 각주만 사라지는 것이 아닙니다. backend는 `routed_agent`로 메시지
틀도 고르므로(`kind_for_agent`), 일지 브랜치가 이 경로로 실패하면 일지 틀이 아니라
분석답변 틀로 렌더됩니다. 오류 문자열에 일지 틀을 씌우지 않는 편이 맞으므로 의도한
방향이지만, 소비자 쪽 영향이 각주 한 줄에 그치지 않는다는 점은 알아 둘 필요가 있습니다.

## 1. 사용자 메모리 (mem0 로컬 모드, #397)

별도 서버나 API 키 없이 기본으로 켜져 있습니다. 텔레그램 `/start`의 두 번째 문항이나 `/risk 안정형|공격형|해제`로 고른 투자 성향이 종목 추천 논조에 반영됩니다(안정형: 변동성 경고 우선, 공격형: 기회 요인 우선). 성향을 고르지 않은 사용자의 추천은 이 기능이 없을 때와 같습니다.

**저장 위치는 로컬뿐입니다.** mem0 라이브러리의 `AsyncMemory`를 NAT 프로세스 안에서 띄우고, qdrant 로컬 파일과 sqlite 이력을 `FINUS_MEM0_STORAGE_DIR`(기본 `finus_nat/.state/mem0`)에 둡니다. NAT의 `mem0_memory`(self-hosted 서버나 `api.mem0.ai`로 가는 HTTP 클라이언트)는 쓰지 않습니다. mem0의 PostHog 텔레메트리, OpenAI 임베딩, 추출 LLM은 코드로 막혀 있습니다(`src/nat_finus_nat/mem0_local.py`).

**저장하는 값은 투자 성향 열거형뿐입니다.** 쓰기 경로는 `src/nat_finus_nat/user_memory.py`의 허용목록 게이트 하나이고, 대화 본문·계좌·금액은 mem0에 닿기 전에 거부됩니다. 에이전트(LLM)는 메모리에 쓸 수 없습니다 — 대화 전문을 매 턴 저장하던 vendor `auto_memory_agent`는 쓰지 않습니다.

흐름:

- 쓰기: 텔레그램 버튼·`/risk` → backend(`backend/user_preferences.py`) → `POST /v1/user-preferences`
- 읽기: backend가 채팅 스레드(`telegram:<chat_id>`)에만 `x-user-id` 헤더를 싣고, 추천 브랜치(`recommend_branch_agent`, `finus_risk_profile_branch`)가 그 사용자의 성향을 코드로 읽어 `configs/agents/recommend_agent.yml`의 논조 지시를 현재 요청 앞에 붙입니다.

끄려면 `.env`에 `FINUS_MEM0_ENABLED=0`을 두고 NAT을 다시 띄웁니다. 이때 `/risk`는 "사용자 메모리가 꺼져 있다"고 안내하고, 저장돼 있던 파일은 지우지 않습니다(다시 켜면 그대로 쓰입니다). 모두 지우려면 NAT을 멈추고 저장 디렉터리를 삭제합니다.

qdrant 로컬 모드는 디렉터리 잠금을 걸어 한 프로세스만 열 수 있습니다. 같은 저장 디렉터리로 NAT을 두 개 띄우면 두 번째가 기동에 실패합니다.

## 2. Kis-trade-MCP

**Kis-Trade-MCP도 기본으로 Docker에서 구동할때 Port3000으로 설정되어있지만, Fin-Us 설정의 기본값(`FINUS_KIS_TRADING_MCP_URL`)이 3300이므로 포트를 3300으로 변경한다.**

NAT의 `finus_account_balance`는 원격 MCP에 `call_tool(tool_name, {api_type, params})`만 넘깁니다. URL은 `FINUS_KIS_TRADING_MCP_URL` 등으로 설정합니다.
