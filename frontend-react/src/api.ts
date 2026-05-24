import axios from 'axios';
import type {
  AccountBalance,
  AgentReportItem,
  AnalysisReport,
  ApiResponse,
  DiaryGenerateResult,
  DiaryItem,
  HealthStatus,
  PortfolioItem,
  TradeHistoryItem,
} from './types';

export function apiErrorMessage(err: unknown): string {
  if (axios.isAxiosError(err)) {
    const detail = err.response?.data?.detail || err.response?.data?.message;
    if (detail) return typeof detail === 'string' ? detail : JSON.stringify(detail);
    if (err.message) return err.message;
  }
  return '서버와 통신 중 오류가 발생했습니다.';
}

async function getData<T>(url: string, params?: Record<string, string>) {
  const response = await axios.get<ApiResponse<T>>(url, { params });
  return response.data.data;
}

async function postData<T>(url: string, body: unknown) {
  const response = await axios.post<ApiResponse<T>>(url, body);
  return response.data.data;
}

export const finUsApi = {
  health: async () => {
    const response = await axios.get<HealthStatus>('/health');
    return response.data;
  },
  news: (stock: string) => getData<{ stock: string; news: string[] }>('/api/v1/news', { stock }),
  analyze: (stock: string, provider: string) =>
    getData<AnalysisReport>('/api/v1/analyze', { stock, provider }),
  trend: (stock: string) =>
    getData<{ stock: string; trend: string }>('/api/v1/trading/trend', { stock }),
  balance: () => getData<AccountBalance>('/api/v1/trading/balance'),
  portfolio: () => getData<PortfolioItem[]>('/api/v1/db/portfolio'),
  trades: () => getData<TradeHistoryItem[]>('/api/v1/db/trades'),
  reports: () => getData<AgentReportItem[]>('/api/v1/db/reports'),
  diaries: () => getData<DiaryItem[]>('/api/v1/db/diary'),
  createDiary: (title: string, content: string) =>
    postData<DiaryItem>('/api/v1/db/diary', { title, content }),
  generateDiary: () => postData<DiaryGenerateResult>('/api/v1/diary/generate', {}),
};
