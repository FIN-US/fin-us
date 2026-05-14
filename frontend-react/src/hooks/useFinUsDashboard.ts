import { useCallback, useEffect, useRef, useState, type FormEvent } from 'react';
import { apiErrorMessage, finUsApi } from '../api';
import { natHealthCheck, streamNatChatCompletion, type NatChatMessage } from '../natChatClient';
import type { AnalysisReport, ChatMessage, DashboardResources } from '../types';

const initialResources: DashboardResources = {
  health: null,
  balance: null,
  portfolio: [],
  trades: [],
  reports: [],
  diaries: [],
};

function createMessage(role: ChatMessage['role'], text: string, id?: string): ChatMessage {
  return {
    id: id ?? crypto.randomUUID(),
    role,
    text,
    createdAt: new Date().toLocaleTimeString('ko-KR', { hour: '2-digit', minute: '2-digit' }),
  };
}

/** NAT base URL: dev uses Vite ``/nat-agent`` proxy; production can set full origin (NAT CORS must allow the site). */
function natBaseUrl(): string {
  const raw = import.meta.env.VITE_FINUS_NAT_URL as string | undefined;
  if (raw?.trim()) return raw.trim().replace(/\/$/, '');
  return '/nat-agent';
}

const finusChatModel = (import.meta.env.VITE_FINUS_CHAT_MODEL as string | undefined)?.trim();

export function useFinUsDashboard() {
  const [stock, setStock] = useState('삼성전자');
  const [provider, setProvider] = useState('openai');
  const [loading, setLoading] = useState(false);
  const [resourceLoading, setResourceLoading] = useState(false);
  const [report, setReport] = useState<AnalysisReport | null>(null);
  const [rawNews, setRawNews] = useState<string[]>([]);
  const [rawTrend, setRawTrend] = useState<string | null>(null);
  const [resources, setResources] = useState<DashboardResources>(initialResources);
  const [error, setError] = useState('');
  const [chatStatus, setChatStatus] = useState<'connecting' | 'open' | 'closed'>('connecting');
  const [chatBusy, setChatBusy] = useState(false);
  const [chatMessages, setChatMessages] = useState<ChatMessage[]>([
    createMessage('system', 'finus_nat(NAT) 채팅을 준비하는 중입니다.'),
  ]);
  const natConversationIdRef = useRef(crypto.randomUUID());
  const natThreadRef = useRef<NatChatMessage[]>([]);
  const natAbortRef = useRef<AbortController | null>(null);
  const natInFlightRef = useRef(false);

  const resetForRequest = useCallback(() => {
    setError('');
    setReport(null);
    setRawNews([]);
    setRawTrend(null);
  }, []);

  const loadResources = useCallback(async () => {
    setResourceLoading(true);
    setError('');
    const [health, balance, portfolio, trades, reports, diaries] = await Promise.allSettled([
      finUsApi.health(),
      finUsApi.balance(),
      finUsApi.portfolio(),
      finUsApi.trades(),
      finUsApi.reports(),
      finUsApi.diaries(),
    ]);

    setResources({
      health: health.status === 'fulfilled' ? health.value : null,
      balance: balance.status === 'fulfilled' ? balance.value : null,
      portfolio: portfolio.status === 'fulfilled' ? portfolio.value : [],
      trades: trades.status === 'fulfilled' ? trades.value : [],
      reports: reports.status === 'fulfilled' ? reports.value : [],
      diaries: diaries.status === 'fulfilled' ? diaries.value : [],
    });

    const failed = [health, balance, portfolio, trades, reports, diaries].some((item) => item.status === 'rejected');
    if (failed) setError('일부 백엔드 엔드포인트 응답을 가져오지 못했습니다.');
    setResourceLoading(false);
  }, []);

  const handleAnalyze = useCallback(
    async (e: FormEvent) => {
      e.preventDefault();
      if (!stock.trim()) return;
      setLoading(true);
      resetForRequest();
      try {
        const data = await finUsApi.analyze(stock.trim(), provider);
        setReport(data);
        setRawNews(data.source_news || []);
        setRawTrend(data.trading_trend);
      } catch (err: unknown) {
        setError(apiErrorMessage(err));
      } finally {
        setLoading(false);
      }
    },
    [provider, resetForRequest, stock],
  );

  const handleFetchData = useCallback(
    async (e: FormEvent) => {
      e.preventDefault();
      if (!stock.trim()) return;
      setLoading(true);
      resetForRequest();
      try {
        const [news, trend] = await Promise.all([
          finUsApi.news(stock.trim()),
          finUsApi.trend(stock.trim()),
        ]);
        setRawNews(news.news);
        setRawTrend(trend.trend);
      } catch (err: unknown) {
        setError(apiErrorMessage(err));
      } finally {
        setLoading(false);
      }
    },
    [resetForRequest, stock],
  );

  const submitDiary = useCallback(
    async (title: string, content: string) => {
      if (!title.trim() || !content.trim()) return;
      setResourceLoading(true);
      setError('');
      try {
        const diary = await finUsApi.createDiary(title.trim(), content.trim());
        setResources((current) => ({ ...current, diaries: [diary, ...current.diaries] }));
      } catch (err: unknown) {
        setError(apiErrorMessage(err));
      } finally {
        setResourceLoading(false);
      }
    },
    [],
  );

  const resetNatConversation = useCallback(() => {
    natAbortRef.current?.abort();
    natAbortRef.current = null;
    natInFlightRef.current = false;
    natThreadRef.current = [];
    natConversationIdRef.current = crypto.randomUUID();
    setChatBusy(false);
    setChatMessages([
      createMessage(
        'system',
        `새 대화를 시작했습니다. (conversation-id=${natConversationIdRef.current.slice(0, 8)}…)`,
      ),
    ]);
  }, []);

  const sendChatMessage = useCallback(
    async (text: string) => {
      const message = text.trim();
      if (!message || natInFlightRef.current) return;
      if (chatStatus !== 'open') {
        setChatMessages((current) => [...current, createMessage('system', 'NAT에 연결되지 않았습니다. finus_nat가 떠 있는지 확인하세요.')]);
        return;
      }

      natInFlightRef.current = true;
      natThreadRef.current = [...natThreadRef.current, { role: 'user', content: message }];
      setChatMessages((current) => [...current, createMessage('user', message)]);

      const assistantId = crypto.randomUUID();
      setChatMessages((current) => [...current, createMessage('server', '', assistantId)]);
      setChatBusy(true);
      natAbortRef.current?.abort();
      const ac = new AbortController();
      natAbortRef.current = ac;

      const base = natBaseUrl();
      let assistantText = '';

      try {
        assistantText = await streamNatChatCompletion({
          baseUrl: base,
          messages: natThreadRef.current,
          conversationId: natConversationIdRef.current,
          model: finusChatModel,
          signal: ac.signal,
          onAssistantDelta: (chunk) => {
            setChatMessages((current) =>
              current.map((m) => (m.id === assistantId ? { ...m, text: m.text + chunk } : m)),
            );
          },
        });
        natThreadRef.current = [...natThreadRef.current, { role: 'assistant', content: assistantText }];
      } catch (err: unknown) {
        const msg = err instanceof Error ? err.message : String(err);
        if (ac.signal.aborted) {
          setChatMessages((current) => [
            ...current.filter((m) => !(m.id === assistantId && !m.text.trim())),
            createMessage('system', '요청이 취소되었습니다.'),
          ]);
        } else {
          setChatMessages((current) => [
            ...current.filter((m) => !(m.id === assistantId && !m.text.trim())),
            createMessage('system', `NAT 오류: ${msg}`),
          ]);
        }
      } finally {
        natInFlightRef.current = false;
        setChatBusy(false);
        natAbortRef.current = null;
      }
    },
    [chatStatus],
  );

  useEffect(() => {
    loadResources();
  }, [loadResources]);

  useEffect(() => {
    const ac = new AbortController();
    setChatStatus('connecting');

    void (async () => {
      const hr = await natHealthCheck(natBaseUrl(), ac.signal);
      if (ac.signal.aborted) return;
      if (hr.ok) {
        setChatStatus('open');
        setChatMessages((current) => [
          ...current,
          createMessage(
            'system',
            `NAT에 연결되었습니다. (${natBaseUrl()}/v1/chat/completions, conversation-id 유지)`,
          ),
        ]);
        return;
      }
      setChatStatus('closed');
      const base = natBaseUrl();
      const devHint = import.meta.env.DEV
        ? `개발 서버는 \`npm run dev\`일 때만 \`/nat-agent\` → NAT 프록시가 붙습니다. NAT를 띄운 뒤 \`VITE_NAT_TARGET\`이 그 주소·포트와 같게 하고 Vite를 재시작하세요(기본 8765, Docker NAT는 보통 8000). 상세: ${hr.detail}`
        : `프로덕션 빌드에서는 \`VITE_FINUS_NAT_URL\`에 NAT 전체 URL을 넣거나, 리버스 프록시로 /nat-agent를 NAT에 넘기세요. 상세: ${hr.detail}`;
      setChatMessages((current) => [
        ...current,
        createMessage(
          'system',
          `NAT /health에 연결하지 못했습니다. (${base}/health) — ${devHint}`,
        ),
      ]);
    })();

    return () => {
      ac.abort();
      natAbortRef.current?.abort();
    };
  }, []);

  return {
    stock,
    setStock,
    provider,
    setProvider,
    loading,
    resourceLoading,
    report,
    rawNews,
    rawTrend,
    resources,
    error,
    chatStatus,
    chatBusy,
    chatMessages,
    handleAnalyze,
    handleFetchData,
    loadResources,
    submitDiary,
    sendChatMessage,
    resetNatConversation,
  };
}
