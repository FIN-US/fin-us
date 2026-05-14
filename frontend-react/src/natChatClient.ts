/**
 * Browser client for NeMo Agent Toolkit (NAT) OpenAI-compatible chat.
 * Matches ``finus-chat`` / ``chat_cli.py``: POST ``/v1/chat/completions`` + ``conversation-id`` header.
 */

export const NAT_CONVERSATION_HEADER = 'conversation-id';

export type NatChatMessage = { role: 'user' | 'assistant' | 'system'; content: string };

export type NatIntermediateStep = { name?: string; type?: string; payload?: unknown };

function parseSseDataLine(
  line: string,
  onDelta: (chunk: string) => void,
  onIntermediate?: (step: NatIntermediateStep) => void,
): void {
  const trimmed = line.trim();
  if (!trimmed) return;

  if (trimmed.startsWith('intermediate_data:')) {
    const raw = trimmed.slice('intermediate_data:'.length).trim();
    try {
      const obj = JSON.parse(raw) as NatIntermediateStep;
      onIntermediate?.(obj);
    } catch {
      /* ignore malformed intermediate */
    }
    return;
  }

  if (trimmed.startsWith('data:')) {
    const raw = trimmed.slice('data:'.length).trim();
    if (raw === '[DONE]') return;
    try {
      const obj = JSON.parse(raw) as {
        code?: string;
        message?: string;
        choices?: { delta?: { content?: string } }[];
      };
      if (obj?.code && obj?.message) {
        throw new Error(obj.message);
      }
      for (const ch of obj.choices || []) {
        const c = ch.delta?.content;
        if (typeof c === 'string' && c) onDelta(c);
      }
    } catch (e) {
      if (e instanceof SyntaxError) return;
      throw e;
    }
  }
}

/** Process buffered SSE lines (newline-delimited). Returns unconsumed tail. */
export function consumeSseBuffer(
  buffer: string,
  onDelta: (chunk: string) => void,
  onIntermediate?: (step: NatIntermediateStep) => void,
): string {
  const parts = buffer.split('\n');
  const tail = parts.pop() ?? '';
  for (const line of parts) {
    parseSseDataLine(line, onDelta, onIntermediate);
  }
  return tail;
}

export async function streamNatChatCompletion(options: {
  /** Base URL or dev proxy path, no trailing slash (e.g. ``/nat-agent`` or ``http://127.0.0.1:8765``). */
  baseUrl: string;
  messages: NatChatMessage[];
  conversationId: string;
  model?: string;
  signal?: AbortSignal;
  onAssistantDelta: (chunk: string) => void;
  onIntermediate?: (step: NatIntermediateStep) => void;
}): Promise<string> {
  const base = options.baseUrl.replace(/\/$/, '');
  const url = `${base}/v1/chat/completions`;
  const body: Record<string, unknown> = {
    messages: options.messages,
    stream: true,
  };
  if (options.model?.trim()) {
    body.model = options.model.trim();
  }

  const res = await fetch(url, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      [NAT_CONVERSATION_HEADER]: options.conversationId.trim(),
    },
    body: JSON.stringify(body),
    signal: options.signal,
  });

  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`;
    try {
      const j = (await res.json()) as { message?: string };
      if (j?.message) detail = j.message;
    } catch {
      /* ignore */
    }
    throw new Error(detail);
  }

  const reader = res.body?.getReader();
  if (!reader) {
    throw new Error('응답 본문을 읽을 수 없습니다.');
  }

  const decoder = new TextDecoder();
  let carry = '';
  let full = '';

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    carry += decoder.decode(value, { stream: true });
    carry = consumeSseBuffer(carry, (c) => {
      full += c;
      options.onAssistantDelta(c);
    }, options.onIntermediate);
  }
  for (const piece of carry.split('\n')) {
    parseSseDataLine(
      piece,
      (c) => {
        full += c;
        options.onAssistantDelta(c);
      },
      options.onIntermediate,
    );
  }

  return full;
}

export type NatHealthResult =
  | { ok: true }
  | { ok: false; detail: string };

/** GET ``{baseUrl}/health``. NAT 미기동·포트 불일치·프록시 없음 등은 ``ok: false`` + ``detail``로 구분. */
export async function natHealthCheck(baseUrl: string, signal?: AbortSignal): Promise<NatHealthResult> {
  const base = baseUrl.replace(/\/$/, '');
  try {
    const res = await fetch(`${base}/health`, { method: 'GET', signal });
    if (res.ok) return { ok: true };
    return { ok: false, detail: `HTTP ${res.status} ${res.statusText}`.trim() };
  } catch (e) {
    const msg = e instanceof Error ? e.message : String(e);
    return { ok: false, detail: msg };
  }
}
