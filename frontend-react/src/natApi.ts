import axios from 'axios';

const natPrefix = (import.meta.env.VITE_NAT_API_PREFIX as string | undefined) || '/nat';

function todayKstTitle() {
  const ymd = new Date().toLocaleDateString('sv-SE', { timeZone: 'Asia/Seoul' });
  return `매매일지 ${ymd}`;
}

const DIARY_AGENT_PROMPT = (title: string) => `매매일지(diary_agent) 작업입니다. diary_agent.yml 도구 순서를 따르세요.

1. mcp-trading-today-orders로 당일(KST) 주문·체결을 조회합니다.
2. 당일 거래가 있으면 mcp-trading-balance-rlz-pl, 없으면 mcp-trading-get-balance로 계좌·보유 정보를 조회합니다.
3. 조회 결과로 매매일지 초안을 작성합니다 (종목명, 거래금액, 변동율, 매매 구분, 실현손익 순, 사용자 의견 공간 포함).
4. finus-save-diary는 호출하지 마세요. 제목 제안: "${title}". 초안 전체를 Final Answer로만 제시하세요.`;

function extractAssistantText(data: unknown): string {
  if (!data || typeof data !== 'object') return '';
  const choices = (data as { choices?: Array<{ message?: { content?: unknown } }> }).choices;
  const content = choices?.[0]?.message?.content;
  if (typeof content === 'string') return content;
  if (content != null) return String(content);
  return JSON.stringify(data, null, 2);
}

/** NAT diary_agent로 초안만 생성합니다. DB 저장은 호출 측에서 backend API로 수행하세요. */
export async function runDiaryAgentDraft(): Promise<{ text: string; title: string; conversationId: string }> {
  const conversationId = crypto.randomUUID();
  const title = todayKstTitle();

  const response = await axios.post(
    `${natPrefix}/v1/chat/completions`,
    {
      messages: [{ role: 'user', content: DIARY_AGENT_PROMPT(title) }],
      stream: false,
    },
    {
      headers: {
        'Content-Type': 'application/json',
        'conversation-id': conversationId,
      },
      timeout: 600_000,
    },
  );

  const payload = response.data as { code?: number; message?: string };
  if (payload?.code && payload?.message) {
    throw new Error(payload.message);
  }

  return {
    text: extractAssistantText(response.data),
    title,
    conversationId,
  };
}
