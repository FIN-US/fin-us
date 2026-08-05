import axios, { type AxiosResponse } from 'axios';
import type {
  AccountBalance,
  AgentReportItem,
  AnalysisReport,
  ApiResponse,
  DiaryItem,
  HealthStatus,
  PortfolioItem,
  TradeHistoryItem,
} from './types';

// status가 success가 아니거나 data가 없는 API 응답을 나타낸다. 사용자에게는
// 백엔드 message/url을 그대로 노출하지 않고(내부 경로 유출 방지), 진단 정보는
// message/console.error 쪽으로만 보낸다.
export class ApiStatusError extends Error {
  constructor(readonly status: string, readonly detail: string | null, readonly url: string) {
    super(`API 오류: status="${status}"${detail ? `, message="${detail}"` : ''} (url=${url})`);
    this.name = 'ApiStatusError';
  }
}

export function apiErrorMessage(err: unknown): string {
  if (axios.isAxiosError(err)) {
    const detail = err.response?.data?.detail || err.response?.data?.message;
    if (detail) return typeof detail === 'string' ? detail : JSON.stringify(detail);
    if (err.message) return err.message;
  }
  if (err instanceof ApiStatusError) {
    console.error(err.message);
    return err.detail ?? '서버가 오류 응답을 반환했습니다.';
  }
  return '서버와 통신 중 오류가 발생했습니다.';
}

function unwrap<T>(response: AxiosResponse<ApiResponse<T>>, url: string): T {
  const { status, data, message } = response.data;
  if (status !== 'success' || data == null) {
    throw new ApiStatusError(status, message ?? null, url);
  }
  return data;
}

async function getData<T>(url: string, params?: Record<string, string>): Promise<T> {
  return unwrap(await axios.get<ApiResponse<T>>(url, { params }), url);
}

async function postData<T>(url: string, body: unknown): Promise<T> {
  return unwrap(await axios.post<ApiResponse<T>>(url, body), url);
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
};
