import React, { useEffect, useRef, useState } from 'react';
import { MessageSquare, SendHorizonal } from 'lucide-react';
import type { ChatMessage } from '../types';

interface ChatPanelProps {
  status: 'connecting' | 'open' | 'closed';
  busy?: boolean;
  messages: ChatMessage[];
  onSend: (message: string) => void;
  onNewConversation?: () => void;
}

const statusLabel = {
  connecting: '연결 중',
  open: '연결됨',
  closed: '닫힘',
};

const ChatPanel: React.FC<ChatPanelProps> = ({ status, busy = false, messages, onSend, onNewConversation }) => {
  const [draft, setDraft] = useState('');
  const scrollAreaRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const el = scrollAreaRef.current;
    if (!el) return;
    const id = requestAnimationFrame(() => {
      el.scrollTop = el.scrollHeight;
    });
    return () => cancelAnimationFrame(id);
  }, [messages]);

  const submit = (e: React.FormEvent) => {
    e.preventDefault();
    onSend(draft);
    setDraft('');
  };

  return (
    <section className="bg-white p-6 rounded-lg shadow-xl border border-slate-100 h-full flex flex-col">
      <div className="flex items-center justify-between mb-5">
        <div className="flex items-center gap-3">
          <div className="p-2 bg-sky-50 rounded-lg"><MessageSquare className="w-5 h-5 text-sky-600" /></div>
          <h3 className="text-slate-900 font-black">finus_nat 채팅</h3>
        </div>
        <div className="flex items-center gap-2">
          {onNewConversation && (
            <button
              type="button"
              onClick={onNewConversation}
              disabled={busy}
              className="text-xs font-bold text-sky-700 hover:underline disabled:opacity-40"
            >
              새 대화
            </button>
          )}
          <span className={`text-xs font-black px-3 py-1 rounded-full ${
            status === 'open' ? 'bg-emerald-50 text-emerald-700' : 'bg-slate-100 text-slate-500'
          }`}>
            {busy ? '응답 중…' : statusLabel[status]}
          </span>
        </div>
      </div>

      <div
        ref={scrollAreaRef}
        className="h-72 overflow-y-auto overflow-x-hidden bg-slate-50 rounded-lg p-4 space-y-3"
      >
        {messages.map((message) => (
          <div
            key={message.id}
            className={`max-w-[85%] rounded-lg px-4 py-3 text-sm ${
              message.role === 'user'
                ? 'ml-auto bg-indigo-600 text-white'
                : message.role === 'server'
                  ? 'bg-white text-slate-700 border border-slate-100'
                  : 'bg-slate-200 text-slate-500'
            }`}
          >
            <div className="text-[10px] opacity-70 mb-1">{message.createdAt}</div>
            <div className="break-words">{message.text}</div>
          </div>
        ))}
      </div>

      <form onSubmit={submit} className="mt-4 flex gap-2">
        <input
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          placeholder="NAT 에이전트에게 메시지 (OpenAI 호환 /v1/chat/completions)"
          disabled={status !== 'open' || busy}
          className="min-w-0 flex-1 rounded-lg border border-slate-200 px-4 py-3 text-sm focus:outline-none focus:ring-4 focus:ring-sky-100 disabled:bg-slate-100"
        />
        <button
          type="submit"
          disabled={status !== 'open' || busy}
          className="inline-flex items-center justify-center rounded-lg bg-sky-600 px-4 text-white disabled:bg-slate-300"
          title="메시지 전송"
        >
          <SendHorizonal className="w-5 h-5" />
        </button>
      </form>
    </section>
  );
};

export default ChatPanel;
