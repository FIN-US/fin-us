# 계획: NAT 인터랙티브 채팅

## Context

현재 프론트엔드 ChatPanel은 WebSocket에 연결되어 있지만, `/api/v1/ws`는 서버→클라이언트 브로드캐스트(SYSTEM_PING, AGENT_ANALYSIS)와 echo 응답만 처리한다. 사용자가 메시지를 보내도 NAT 라우터 워크플로우로 전달되지 않는다.

NAT는 이미 `POST /v1/chat/completions`로 OpenAI 호환 API를 제공하고 있고, 백엔드에는 NAT 호출과 오류 변환 로직이 있다. 목표는 기존 브로드캐스트 채널을 깨지 않고 사용자↔NAT 채팅을 별도 경로로 완성하는 것이다.

## 아키텍처 결론: 백엔드 경유

직접 연결(프론트엔드 → NAT)은 기본 경로로 쓰지 않는다:
- Docker compose에서 NAT는 host `localhost:8001`로 노출될 수 있지만, 브라우저용 public API 경계가 아니다.
- NAT CORS/auth/session 정책을 프론트가 직접 떠안게 된다.
- NAT 모델명, 오류 메시지, timeout, 로그 정책이 프론트로 새어 나온다.
- 향후 사용자별 세션, 권한, 저장 정책을 백엔드에서 통제하기 어렵다.

**채택 경로:** `프론트엔드 WebSocket → 백엔드 /api/v1/ws/chat → NAT chat service → NAT`

기존 `/api/v1/ws`는 스케줄러 브로드캐스트 전용으로 유지한다. 채팅 연결은 `ws_manager`에 등록하지 않는 별도 `/api/v1/ws/chat` 엔드포인트에서 처리한다. 이렇게 해야 `AGENT_ANALYSIS`, `SYSTEM_PING` 브로드캐스트와 사용자별 채팅 응답이 섞이지 않는다.

## 메시지 프로토콜

**클라이언트 → 백엔드 (CHAT_REQUEST):**
```json
{ "type": "CHAT_REQUEST", "message": "삼성전자 지금 사도 될까요?", "request_id": "<uuid>" }
```

**백엔드 → 클라이언트 (CHAT_STATUS):**
```json
{ "type": "CHAT_STATUS", "request_id": "<uuid>", "status": "thinking" }
```

**백엔드 → 클라이언트 (CHAT_RESPONSE):**
```json
{ "type": "CHAT_RESPONSE", "request_id": "<uuid>", "message": "NAT 응답 텍스트", "role": "assistant" }
```

**백엔드 → 클라이언트 (CHAT_ERROR):**
```json
{ "type": "CHAT_ERROR", "request_id": "<uuid>", "message": "오류 설명" }
```

기존 `/api/v1/ws`의 `AGENT_ANALYSIS`, `SYSTEM_PING` 브로드캐스트는 변경하지 않는다. ChatPanel은 기본적으로 `/api/v1/ws/chat`만 사용하므로 브로드캐스트 이벤트를 채팅 말풍선으로 표시하지 않는다.

## 변경 파일 목록

| 파일 | 변경 범위 |
|------|----------|
| `backend/services.py` | NAT 채팅용 public 함수 추가, messages 배열 지원 |
| `backend/main.py` | `json`, NAT 채팅 함수 import, `/api/v1/ws/chat` 엔드포인트 추가 |
| `frontend-react/src/hooks/useFinUsDashboard.ts` | 채팅 WebSocket URL 분리, `chatPending` 상태 추가, JSON 프로토콜 전송/수신 처리 |
| `frontend-react/src/components/ChatPanel.tsx` | `pending` prop 추가, input/button disabled, 스피너 |
| `frontend-react/src/App.tsx` | `chatPending` 디스트럭처링 + ChatPanel prop 전달 |

## 구현 상세

### 1. `backend/services.py`

`_llm_nat_chat(user_msg: str)`는 기존 분석 경로에서 계속 쓴다. 채팅용으로는 대화 히스토리를 전달할 수 있는 public 함수를 추가한다.

```python
NatChatMessage = dict[str, str]


async def chat_with_nat_messages(messages: list[NatChatMessage]) -> str:
    url = f"{NAT_BASE_URL}/v1/chat/completions"
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(120.0)) as client:
            resp = await client.post(
                url,
                json={
                    "model": NAT_CHAT_MODEL,
                    "messages": messages,
                    "temperature": 0.2,
                    "stream": False,
                },
            )
            _log_nat_response(resp)
    except httpx.RequestError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"NAT에 연결할 수 없습니다 ({url}): {exc}",
        ) from exc

    if resp.status_code >= 400:
        raise HTTPException(status_code=resp.status_code, detail=resp.text)

    try:
        payload = resp.json()
    except ValueError as exc:
        logger.exception(
            "Failed to parse NAT response JSON: status_code=%s",
            resp.status_code,
        )
        logger.debug(
            "NAT response body preview: %s",
            resp.text[:_NAT_RESPONSE_LOG_PREVIEW_CHARS],
        )
        raise HTTPException(
            status_code=502,
            detail=f"NAT JSON 파싱 실패: {exc}; body[:800]={resp.text[:800]!r}",
        ) from exc

    if not isinstance(payload, dict):
        raise HTTPException(status_code=502, detail="NAT 응답이 JSON 객체가 아닙니다.")

    try:
        return _nat_message_from_payload(payload)
    except (KeyError, IndexError, TypeError) as exc:
        body_snip = resp.text[:1200] if resp.text else ""
        raise HTTPException(
            status_code=502,
            detail=(
                f"NAT 응답 형식 오류 ({exc}). NAT_CHAT_MODEL이 NAT 서비스에서 쓰는 모델과 일치하는지 확인하세요. "
                f"body[:1200]={body_snip!r}"
            ),
        ) from exc
```

기존 `_llm_nat_chat(user_msg)`는 아래처럼 새 함수를 감싸도록 단순화할 수 있다.

```python
async def _llm_nat_chat(user_msg: str) -> str:
    return await chat_with_nat_messages([{"role": "user", "content": user_msg}])
```

---

### 2. `backend/main.py`

**imports (파일 상단):**
```python
import json  # 추가
```

**services import 블록에 추가:**
```python
from .services import (
    ...,
    chat_with_nat_messages,  # 추가
)
```

**채팅 전용 WebSocket 엔드포인트 추가:**
```python
CHAT_HISTORY_LIMIT = 12


@app.websocket("/api/v1/ws/chat")
async def chat_websocket_endpoint(websocket: WebSocket):
    """사용자와 NAT 라우터 워크플로우 간의 1:1 채팅 WebSocket입니다."""
    await websocket.accept()
    messages: list[dict[str, str]] = []
    try:
        while True:
            raw = await websocket.receive_text()
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                await websocket.send_json({
                    "type": "CHAT_ERROR",
                    "request_id": "",
                    "message": "JSON 메시지만 전송할 수 있습니다.",
                })
                continue

            if not isinstance(payload, dict) or payload.get("type") != "CHAT_REQUEST":
                await websocket.send_json({
                    "type": "CHAT_ERROR",
                    "request_id": "",
                    "message": "지원하지 않는 채팅 메시지 형식입니다.",
                })
                continue

            request_id = str(payload.get("request_id") or "")
            user_text = str(payload.get("message") or "").strip()
            if not user_text:
                await websocket.send_json({
                    "type": "CHAT_ERROR",
                    "request_id": request_id,
                    "message": "빈 메시지는 전송할 수 없습니다.",
                })
                continue

            await websocket.send_json({
                "type": "CHAT_STATUS",
                "request_id": request_id,
                "status": "thinking",
            })

            messages.append({"role": "user", "content": user_text})
            messages = messages[-CHAT_HISTORY_LIMIT:]
            try:
                nat_reply = await chat_with_nat_messages(messages)
                messages.append({"role": "assistant", "content": nat_reply})
                messages = messages[-CHAT_HISTORY_LIMIT:]
                await websocket.send_json({
                    "type": "CHAT_RESPONSE",
                    "request_id": request_id,
                    "message": nat_reply,
                    "role": "assistant",
                })
            except Exception as exc:
                detail = getattr(exc, "detail", str(exc))
                logger.error("NAT chat error: %s", exc)
                await websocket.send_json({
                    "type": "CHAT_ERROR",
                    "request_id": request_id,
                    "message": str(detail),
                })

    except WebSocketDisconnect:
        logger.info("NAT chat WebSocket disconnected.")
    except Exception as exc:
        logger.error("NAT chat WebSocket error: %s", exc)
```

기존 `/api/v1/ws` 핸들러와 `ws_manager.py`는 그대로 둔다.

---

### 3. `frontend-react/src/hooks/useFinUsDashboard.ts`

**채팅용 WebSocket URL 분리:**
```typescript
function chatWebsocketUrl() {
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  return `${protocol}//${window.location.host}/api/v1/ws/chat`;
}
```

**`chatPending` state 추가 (line 42 이후):**
```typescript
const [chatPending, setChatPending] = useState(false);
```

**`sendChatMessage` 재작성 (lines 136–147):**
```typescript
const sendChatMessage = useCallback((text: string) => {
  const message = text.trim();
  if (!message || chatPending) return;
  setChatMessages((current) => [...current, createMessage('user', message)]);

  if (socketRef.current?.readyState !== WebSocket.OPEN) {
    setChatMessages((current) => [
      ...current,
      createMessage('system', 'WebSocket이 연결되어 있지 않습니다.'),
    ]);
    return;
  }

  setChatPending(true);
  const requestId = crypto.randomUUID();
  socketRef.current.send(
    JSON.stringify({ type: 'CHAT_REQUEST', message, request_id: requestId }),
  );
}, [chatPending]);
```

**`socket.onmessage` 디스패처로 교체 (lines 163–164):**
```typescript
socket.onmessage = (event) => {
  try {
    const payload = JSON.parse(event.data as string);
    const msgType = payload?.type;

    if (msgType === 'CHAT_STATUS') {
      return;
    } else if (msgType === 'CHAT_RESPONSE') {
      setChatPending(false);
      setChatMessages((current) => [...current, createMessage('server', payload.message ?? '')]);
    } else if (msgType === 'CHAT_ERROR') {
      setChatPending(false);
      setChatMessages((current) => [...current, createMessage('system', payload.message ?? 'NAT 오류')]);
    } else {
      setChatMessages((current) => [...current, createMessage('server', event.data as string)]);
    }
  } catch {
    setChatMessages((current) => [...current, createMessage('server', event.data as string)]);
  }
};
```

**종료/오류 시 pending 해제:**
```typescript
socket.onerror = () => {
  setChatPending(false);
  setChatMessages((current) => [...current, createMessage('system', 'WebSocket 오류가 발생했습니다.')]);
};

socket.onclose = () => {
  setChatPending(false);
  setChatStatus('closed');
  if (reportClose) {
    setChatMessages((current) => [...current, createMessage('system', 'WebSocket 연결이 닫혔습니다.')]);
  }
};
```

**return 객체에 추가 (line 201 이후):**
```typescript
chatPending,
```

---

### 4. `frontend-react/src/components/ChatPanel.tsx`

**props 타입에 `pending` 추가:**
```typescript
interface ChatPanelProps {
  status: 'connecting' | 'open' | 'closed';
  messages: ChatMessage[];
  onSend: (message: string) => void;
  pending?: boolean;
}
```

**input과 button에 `pending` 적용:**
```tsx
<input
  ...
  placeholder={pending ? 'NAT가 응답 중...' : '메시지를 입력하세요'}
  disabled={pending}
/>
<button
  type="submit"
  disabled={status !== 'open' || !!pending}
>
  {pending
    ? <Loader2 className="w-5 h-5 animate-spin" />
    : <SendHorizonal className="w-5 h-5" />}
</button>
```

> `Loader2`는 lucide-react에 있으며 이미 패키지가 설치되어 있다. import에 추가만 하면 된다.

---

### 5. `frontend-react/src/App.tsx`

**hook 반환값 디스트럭처링에 추가:**
```typescript
chatPending,
```

**ChatPanel JSX에 prop 전달:**
```tsx
<ChatPanel
  status={chatStatus}
  messages={chatMessages}
  onSend={sendChatMessage}
  pending={chatPending}
/>
```

## 변경하지 않는 파일

- `backend/ws_manager.py` — 브로드캐스트 구조 그대로 유지
- `backend/schemas.py` — 새 Pydantic 모델 불필요
- `frontend-react/src/types.ts` — `ChatMessage.role: 'system' | 'user' | 'server'` 이미 적합

## 에러 처리

| 상황 | 처리 |
|------|------|
| 빈 메시지 | 백엔드에서 CHAT_ERROR 반환 |
| WebSocket 미연결 상태로 전송 | 훅 내부 guard → system 메시지 |
| NAT 연결 불가 (RequestError) | HTTPException(502) → CHAT_ERROR → system 버블 |
| NAT timeout (120s) | 동일 경로 |
| 응답 수신 전 WebSocket 종료 | `WebSocketDisconnect` 처리, 프론트 `onclose`에서 `chatPending=false` |
| WebSocket 오류 | 프론트 `onerror`에서 `chatPending=false` |

## 검증

```bash
# 1. 백엔드 테스트
uv run --project backend pytest backend/tests/

# 2. 타입 체크
cd frontend-react && npx tsc --noEmit

# 3. 개발 서버 기동 후 브라우저에서 확인
#    - ChatPanel에서 메시지 입력 → 전송 버튼 클릭
#    - 전송 즉시 버튼 스피너로 변경, input 비활성화 확인
#    - NAT 응답 도착 후 server 버블 표시, 입력 활성화 확인
#    - WebSocket 오류/종료 후 pending 상태가 해제되는지 확인
#    - /api/v1/ws 브로드캐스트 채널과 /api/v1/ws/chat 채팅 채널이 분리되어 있는지 확인
```

추가 테스트 항목:
- `chat_with_nat_messages()`가 `messages` 배열을 NAT 요청 본문에 그대로 전달하는지 검증
- `/api/v1/ws/chat`에서 빈 메시지, 잘못된 JSON, NAT 오류가 `CHAT_ERROR`로 반환되는지 검증
- 기존 `/api/v1/ws` echo 및 scheduler broadcast 테스트가 깨지지 않는지 검증
