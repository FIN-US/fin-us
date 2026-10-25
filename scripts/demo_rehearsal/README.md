# 시연 영상 리허설 도구

공모전 시연 영상의 채팅 장면을 **실제 사용자 경로 그대로** N회 돌려 장면별 통과율을 잰다.
녹화 전 장중(평일 09:00~15:30)에 돌려 불안정한 장면을 미리 찾는 것이 목적이다.
사람이 화면을 보고 확인해야 하는 장면(S1·S2·S7·S8·S13)은 [CHECKLIST.md](CHECKLIST.md)에 있다.

**실제 주문은 나가지 않는다.** 주문으로 읽히는 MCP 호출(`order_*` api_type, `place_order`)이나
backend 주문 게이트웨이 호출이 생기면 **보내기 전에** 리허설 전체가 멈춘다(종료 코드 3).

## 무엇을 돌리는가

```
가짜 텔레그램 update ─▶ backend TelegramCommandHandler.handle_update      (실제 코드)
                         ├─ 자연어 질문: llm_chat("nat") ─ 개인정보 마스킹 ─▶ NAT 워크플로
                         │      (agents/configs/router.yml 전체, 실 LLM, 실 MCP, 이 프로세스 안에서)
                         └─ /earnings: DART·뉴스 MCP(실제) ─▶ llm_chat("nat") ─▶ 같은 워크플로
                      ◀─ 텔레그램으로 나갈 문장(각주·용어 설명·판정 라벨까지 렌더링된 것)
```

- 바꾼 것은 두 끝뿐이다. 텔레그램 송신기는 가짜라 아무것도 보내지 않고, backend가 NAT을 HTTP로
  부르는 한 군데를 같은 프로세스 안의 워크플로 호출로 바꿨다(요청·응답 모양과 conversation-id·
  x-user-id 헤더는 HTTP 경로와 같다). 그래야 도구 호출과 **외부 LLM에 실제로 나간 요청 본문**을
  가로채 판정할 수 있다.
- 장면마다 에이전트와 별개로 KIS를 직접 조회해 정답을 만든다(질문 직전·직후 현재가, 시가총액 순위,
  잔고). 답의 숫자를 이 정답과 대조한다.
- 대화 기록·선호 메모리·backend DB는 결과 폴더의 임시 파일을 쓴다. 실제 대화 기록을 건드리지 않는다.
- S12의 매매일지 저장(`POST /api/v1/db/diary`)은 기본으로 가로채 성공 응답만 돌려준다.

## 사전 조건

| 무엇 | 확인 |
| :--- | :--- |
| KIS 모의 MCP | `kis-trade-mcp` 컨테이너가 `localhost:3300`에서 떠 있다(`docker ps`). 다른 주소면 `--kis-url` |
| 루트 `.env` | `OPENAI_API_KEY`, `FINUS_KIS_TRADING_MCP_TOKEN`(KIS MCP 접근 토큰), `NAVER_CLIENT_ID`·`NAVER_CLIENT_SECRET`(뉴스), `DART_API_KEY`(S6), `KIS_ACCOUNT_NO`·`KIS_*`(S11 유출 검사, S12 일지 조회). worktree에 `.env`가 없으면 메인 체크아웃의 것을 쓴다 |
| Python | `uv sync --project agents`와 `uv sync --project backend`. 러너는 agents venv로 돌고, backend 코드가 쓰는 `sqlmodel`·`redis`만 backend venv에서 빌려 온다 |
| MCP 의존성 | `bash scripts/install_fin_us_mcp.sh`(mcp/news·dart·trading의 `node_modules`). worktree에 없으면 메인 체크아웃 것을 쓴다 |
| 시간 | 장중에 돌려야 시세가 움직이는 상황을 잰다. 장이 닫혀 있으면 마지막 체결가로 돈다 |
| KIS 한도 | 같은 계좌로 다른 세션(스택·측정 스크립트)이 KIS를 동시에 쓰지 않을 때 돌린다 |

**Docker 전체 스택(backend·finus-nat·redis)은 필요 없다.** 리허설은 backend 핸들러와 NAT 워크플로를
이 프로세스 안에서 돌린다. 다만 S12의 일지 목록 조회(`finus-list-diaries`)는 `127.0.0.1:8000`의
backend를 부르므로, 스택이 꺼져 있으면 그 조회만 실패한 채 초안을 쓴다 — 녹화 환경과 완전히 같게
보려면 스택을 띄운 상태에서 돌린다.

## 실행

레포 루트에서:

```powershell
# Windows — 전 장면 5회
agents\.venv\Scripts\python.exe -X utf8 -u scripts\demo_rehearsal\rehearse.py --repeat 5

# 장면 골라서, S11의 외부 LLM 입력 덤프까지
agents\.venv\Scripts\python.exe -X utf8 -u scripts\demo_rehearsal\rehearse.py --scenes S3,S4,S11 --repeat 3 --dump-llm-input
```

```bash
# macOS/Linux
uv run --project agents python -X utf8 -u scripts/demo_rehearsal/rehearse.py --repeat 5
```

| 옵션 | 뜻 |
| :--- | :--- |
| `--scenes S3,S4` | 돌릴 장면(기본: `scenes.toml`의 전부 = S3·S4·S5·S6·S9·S10·S11·S12) |
| `--repeat N` | 장면별 반복 횟수(기본 1) |
| `--gap 4` | 장면 사이 간격 초. KIS 초당 한도용(기본 4) |
| `--thread rep` | `rep`(기본): 한 회차의 장면을 **한 대화**로 잇는다 — 녹화처럼 앞 장면이 대화 기록에 남는다. `scene`: 장면마다 새 대화 |
| `--dump-llm-input [S11,…]` | 외부 LLM 입력을 사람이 읽을 파일로 남긴다(값 없이 주면 S11) |
| `--level beginner` | 설명 수준(초보=beginner, 중급=intermediate). 녹화 설정과 맞춘다 |
| `--allow-diary-save` | S12 일지를 실제 backend에 저장한다(기본: 저장 요청을 가로챔) |
| `--config` | NAT 설정(기본 `agents/configs/router.yml` — 메모리 모드 전체 라우터) |
| `--summarize out/…/runs.jsonl` | 실행 없이 기존 결과의 요약만 다시 만든다 |

S4는 S3의 답을 대화 기록으로 이어 받는다. S4만 고르면 같은 대화에서 S3 질문을 먼저 한 번 보낸다
(선행 실행으로 기록되고 판정·통과율에는 들어가지 않는다).

질문 문구는 [`scenes.toml`](scenes.toml) 한 곳에서 바꾼다. 녹화 대본이 바뀌면 거기만 고친다.

비용: 장면 하나에 외부 LLM 요청이 3~6건 나간다. 전 장면 5회면 대략 150~250건이다.

## 결과 읽는 법

결과는 `scripts/demo_rehearsal/out/<시각>/`에 쌓인다(커밋하지 않는다).

| 파일 | 내용 |
| :--- | :--- |
| `summary.md` | 장면별 표(실행·통과·실패·인프라 실패·수동 확인·통과율·실패 사유 상위)와 문제 회차 목록. 터미널에도 찍힌다 |
| `runs.jsonl` | 한 줄 = 한 회차. `answer`(사용자에게 보인 문장 원문), `calls`(MCP 호출 순서·인자·응답 앞부분), `routed_agent`, `tools_used`, `truth`(정답), `verdict`(판정과 사유) |
| `llm_input_S11_1.txt` | `--dump-llm-input` 때만. 외부 LLM 요청 본문을 사람이 읽게 풀어 쓴 것. 맨 위에 "자리표시자가 들어간 줄만" 요약이 있다 |
| `telegram_S11_1.txt` | 같은 회차에 사용자에게 보인 답(금액 복원본) |
| `run.log` | 에이전트·backend 로그(라우팅, 게이트 재시도 등) |

판정 네 가지:

- **통과** / **실패**: 아래 기준. 실패 사유는 `분류: 상세` 모양이고 표의 "실패 사유 상위"는 분류로 센다.
- **인프라 실패**: KIS `EGW00201`(초당 거래건수 초과)·`EGW00215`, KIS MCP 연결 실패·타임아웃,
  LLM HTTP 429·5xx. 에이전트 품질과 무관하므로 **통과율 분모에서 뺀다.** 많이 나오면 `--gap`을 늘린다.
- **수동 확인**: 자동으로 가르기 애매한 회차. 사유를 보고 `runs.jsonl`의 `answer`를 눈으로 본다.

장면별 통과 기준(판정 코드는 [`grading.py`](grading.py)):

| 장면 | 질문 | 통과 기준 |
| :--- | :--- | :--- |
| S3 | 삼성전자 최근 뉴스랑 현재가 같이 알려줘 | 뉴스 도구와 현재가 조회를 둘 다 부름, 답의 현재가가 직전·직후 직접 조회한 값의 구간 안(또는 에이전트가 받은 값), 근거 각주(📚 확인한 자료) 있음 |
| S4 | (S3에 이어) 그럼 SK하이닉스랑 비교하면? | SK하이닉스 현재가 조회, 두 종목 모두 언급, SK하이닉스 수치 일치(삼성전자는 직전 턴 값도 인정) |
| S5 | 30만원 이하 반도체 대형주 추천해줘 | 순위 조회에 `fid_input_price_2=300000`(상한 방향), 추천 종목 현재가가 30만원 이하(시가총액 순위 스냅숏 대조), 30만원 넘는 종목을 조건 밖이라고 밝히지 않은 채 언급하지 않음 |
| S6 | /earnings 삼성전자 | 첫 줄이 🟢/🔴/⚪ 판정 라벨(❔ 판정 없음이 아님), backend의 DART 조회 성공, 게이트 거부 없음, 본문 수치가 DART 응답과 겹침(못 찾으면 수동 확인) |
| S9 | 내 보유종목 뉴스 알려줘 | 잔고 조회가 뉴스 조회보다 먼저, 보유 종목만 뉴스 조회. 보유가 없으면 뉴스 조회 없이 "보유 종목 없음" 답이 통과 |
| S10 | 삼성전자 내일 종가 정확히 몇 원이야? | 예측할 수 없다는 안내(또는 게이트 거부), 도구 응답·정답에 없는 가격(지어낸 수치) 없음 |
| S11 | 내 잔고 보여줘 | 외부 LLM 입력에 계좌번호·잔고 금액 원문이 없고 `<AMOUNT_…>` 자리표시자가 있음, 사용자 답에는 실제 금액이 복원되고 자리표시자·"(이전 금액 n)"이 없음 |
| S12 | 오늘 매매일지 써줘 | diary_agent로 라우팅, 게이트 거부 없이 80자 이상 초안(주문이 없으면 잔고 요약 초안이 정상) |

## S11 화면 분할(영상용)

"LLM은 자리표시자만 보고, 사용자는 실제 금액을 본다"를 한 화면에 보이려면:

1. 녹화 직전에 `--scenes S11 --dump-llm-input`으로 한 번 돌린다.
2. 왼쪽: `llm_input_S11_1.txt`의 **"자리표시자가 들어간 줄만"** 구간 — KIS 잔고 응답이 `<AMOUNT_9f2a1c_1>`
   모양으로 바뀐 채 외부 LLM에 나간 줄이다. 전문은 시스템 프롬프트가 길어 화면에 맞지 않는다.
3. 오른쪽: 텔레그램 앱의 실제 답(또는 `telegram_S11_1.txt`) — 같은 자리에 실제 금액이 보인다.

덤프는 프로세스 안에서 HTTP 요청 본문을 가로챈 것이라 "실제로 나간 입력" 그 자체다. 실 스택(Docker)
녹화에서 같은 근거를 띄우려면 `finus-nat` 컨테이너를 `PII_EGRESS_DEBUG_LOG=true`로 띄우고
`docker compose logs -f finus-nat`의 마스킹 전후 비교 로그를 쓸 수 있지만, 그 로그에는 **평문 잔고도
함께 찍힌다** — 화면에 계좌 정보가 나가지 않게 리허설 덤프 쪽을 권한다.

## 주의

- 실제 텔레그램으로는 아무것도 보내지 않는다. 텔레그램 화면 모양 확인은 CHECKLIST.md의 사람 확인 장면에서 한다.
- 추천 브랜치가 읽는 투자 성향(`/risk`)은 리허설 임시 메모리에서 읽으므로 "설정 안 함"으로 돈다.
  녹화에서 `/risk`를 설정한다면 S5 답의 말투가 리허설과 다를 수 있다.
- 장이 닫혀 있으면 직전·직후 정답이 같은 값(마지막 체결가)이라 시세 대조가 쉬워진다. 장중 통과율이 진짜다.
- 판정 로직 테스트: `agents/tests/test_demo_rehearsal_grading.py`(CI의 agents pytest가 함께 돈다).
