import { useCallback, useEffect, useRef, useState, type FormEvent } from 'react';
import { apiErrorMessage, finUsApi } from '../api';
import type { AnalysisReport, ChatMessage, DashboardResources, EndpointStatuses } from '../types';

const initialEndpointStatuses: EndpointStatuses = {
  health: 'unknown',
  balance: 'unknown',
  portfolio: 'unknown',
  trades: 'unknown',
  reports: 'unknown',
  diaries: 'unknown',
};

const initialResources: DashboardResources = {
  health: null,
  balance: null,
  portfolio: [],
  trades: [],
  reports: [],
  diaries: [],
};

function createMessage(role: ChatMessage['role'], text: string): ChatMessage {
  return {
    id: crypto.randomUUID(),
    role,
    text,
    createdAt: new Date().toLocaleTimeString('ko-KR', { hour: '2-digit', minute: '2-digit' }),
  };
}

function websocketUrl() {
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  return `${protocol}//${window.location.host}/api/v1/ws`;
}

export function useFinUsDashboard() {
  const [stock, setStock] = useState('삼성전자');
  const [provider, setProvider] = useState('openai');
  const [loading, setLoading] = useState(false);
  const [resourceLoading, setResourceLoading] = useState(false);
  const [report, setReport] = useState<AnalysisReport | null>(null);
  const [rawNews, setRawNews] = useState<string[]>([]);
  const [rawTrend, setRawTrend] = useState<string | null>(null);
  const [resources, setResources] = useState<DashboardResources>(initialResources);
  const [endpointStatuses, setEndpointStatuses] = useState<EndpointStatuses>(initialEndpointStatuses);
  const [error, setError] = useState('');
  const [chatStatus, setChatStatus] = useState<'connecting' | 'open' | 'closed'>('connecting');
  const [chatMessages, setChatMessages] = useState<ChatMessage[]>([
    createMessage('system', 'WebSocket 연결을 준비하고 있습니다.'),
  ]);
  const socketRef = useRef<WebSocket | null>(null);

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

    setEndpointStatuses({
      health: health.status === 'fulfilled' ? 'ok' : 'fail',
      balance: balance.status === 'fulfilled' ? 'ok' : 'fail',
      portfolio: portfolio.status === 'fulfilled' ? 'ok' : 'fail',
      trades: trades.status === 'fulfilled' ? 'ok' : 'fail',
      reports: reports.status === 'fulfilled' ? 'ok' : 'fail',
      diaries: diaries.status === 'fulfilled' ? 'ok' : 'fail',
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

  const sendChatMessage = useCallback((text: string) => {
    const message = text.trim();
    if (!message) return;
    setChatMessages((current) => [...current, createMessage('user', message)]);

    if (socketRef.current?.readyState !== WebSocket.OPEN) {
      setChatMessages((current) => [...current, createMessage('system', 'WebSocket이 연결되어 있지 않습니다.')]);
      return;
    }

    socketRef.current.send(message);
  }, []);

  useEffect(() => {
    loadResources();
  }, [loadResources]);

  useEffect(() => {
    const socket = new WebSocket(websocketUrl());
    let reportClose = true;
    socketRef.current = socket;
    setChatStatus('connecting');

    socket.onopen = () => {
      setChatStatus('open');
      setChatMessages((current) => [...current, createMessage('system', 'WebSocket 연결이 열렸습니다.')]);
    };
    socket.onmessage = (event) => {
      setChatMessages((current) => [...current, createMessage('server', event.data)]);
    };
    socket.onerror = () => {
      setChatMessages((current) => [...current, createMessage('system', 'WebSocket 오류가 발생했습니다.')]);
    };
    socket.onclose = () => {
      setChatStatus('closed');
      if (reportClose) {
        setChatMessages((current) => [...current, createMessage('system', 'WebSocket 연결이 닫혔습니다.')]);
      }
    };

    return () => {
      reportClose = false;
      socket.close();
      socketRef.current = null;
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
    endpointStatuses,
    error,
    chatStatus,
    chatMessages,
    handleAnalyze,
    handleFetchData,
    loadResources,
    submitDiary,
    sendChatMessage,
  };
}
