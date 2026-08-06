import { renderHook, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { finUsApi } from '../api';
import { useFinUsDashboard } from './useFinUsDashboard';

// #71 회귀 가드 (판정 계층).
//
// #71의 버그는 "fetch에 실패했는데 초록"이었고, 원인은 `resources.portfolio.length >= 0`
// 같은 항상-참 술어로 상태를 판정한 것이었다. 6aa72e1이 그 판정 로직을 BackendPanel에서
// 이 훅의 loadResources로 옮겼으므로, 회귀 가드도 여기에 있어야 한다.
// BackendPanel.test.tsx는 판정 결과 -> 화면 매핑만 덮는다.
//
// 핵심 조건: 요청이 거부됐는데 resources.portfolio는 []가 된다.
// 빈 배열은 length >= 0을 만족하므로, 술어가 되돌아오면 이 테스트가 red가 된다.

vi.mock('../api', () => ({
  finUsApi: {
    health: vi.fn(),
    balance: vi.fn(),
    portfolio: vi.fn(),
    trades: vi.fn(),
    reports: vi.fn(),
    diaries: vi.fn(),
    analyze: vi.fn(),
    news: vi.fn(),
    trend: vi.fn(),
    createDiary: vi.fn(),
  },
  apiErrorMessage: (err: unknown) => String(err),
}));

// 훅은 마운트 시 WebSocket을 연다. jsdom에는 WebSocket 구현이 없으므로 스텁이 필요하다.
class MockWebSocket {
  static readonly OPEN = 1;
  onopen: ((this: WebSocket, ev: Event) => unknown) | null = null;
  onmessage: ((this: WebSocket, ev: MessageEvent) => unknown) | null = null;
  onerror: ((this: WebSocket, ev: Event) => unknown) | null = null;
  onclose: ((this: WebSocket, ev: CloseEvent) => unknown) | null = null;
  readyState = 0;
  constructor(public readonly url: string) {}
  send() {}
  close() {}
}

beforeEach(() => {
  vi.stubGlobal('WebSocket', MockWebSocket);
  vi.mocked(finUsApi.health).mockResolvedValue({ status: 'ok', nat_base_url: 'http://nat' });
  vi.mocked(finUsApi.balance).mockResolvedValue({} as never);
  vi.mocked(finUsApi.portfolio).mockResolvedValue([]);
  vi.mocked(finUsApi.trades).mockResolvedValue([]);
  vi.mocked(finUsApi.reports).mockResolvedValue([]);
  vi.mocked(finUsApi.diaries).mockResolvedValue([]);
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.resetAllMocks();
});

describe('useFinUsDashboard - 엔드포인트 상태 판정', () => {
  it('#71: portfolio 조회가 거부되면 resources가 빈 배열이어도 fail로 판정한다', async () => {
    vi.mocked(finUsApi.portfolio).mockRejectedValue(new Error('boom'));

    const { result } = renderHook(() => useFinUsDashboard());

    await waitFor(() => expect(result.current.endpointStatuses.portfolio).toBe('fail'));
    // 거부됐는데 빈 배열 — 이 조합이 정확히 #71의 조건이다.
    expect(result.current.resources.portfolio).toEqual([]);
    // 성공한 다른 엔드포인트는 영향을 받지 않는다.
    expect(result.current.endpointStatuses.trades).toBe('ok');
    expect(result.current.endpointStatuses.health).toBe('ok');
  });

  it('#71: 배열 계열 엔드포인트 전부가 거부되면 모두 fail로 판정한다', async () => {
    vi.mocked(finUsApi.portfolio).mockRejectedValue(new Error('boom'));
    vi.mocked(finUsApi.trades).mockRejectedValue(new Error('boom'));
    vi.mocked(finUsApi.reports).mockRejectedValue(new Error('boom'));
    vi.mocked(finUsApi.diaries).mockRejectedValue(new Error('boom'));

    const { result } = renderHook(() => useFinUsDashboard());

    await waitFor(() => expect(result.current.endpointStatuses.portfolio).toBe('fail'));
    expect(result.current.endpointStatuses.trades).toBe('fail');
    expect(result.current.endpointStatuses.reports).toBe('fail');
    expect(result.current.endpointStatuses.diaries).toBe('fail');
    expect(result.current.resources.trades).toEqual([]);
    expect(result.current.resources.reports).toEqual([]);
    expect(result.current.resources.diaries).toEqual([]);
    expect(result.current.error).toBe('일부 백엔드 엔드포인트 응답을 가져오지 못했습니다.');
  });

  it('전부 성공하면 모두 ok로 판정하고 에러 메시지를 남기지 않는다', async () => {
    const { result } = renderHook(() => useFinUsDashboard());

    await waitFor(() => expect(result.current.endpointStatuses.portfolio).toBe('ok'));
    expect(result.current.endpointStatuses).toEqual({
      health: 'ok',
      balance: 'ok',
      portfolio: 'ok',
      trades: 'ok',
      reports: 'ok',
      diaries: 'ok',
    });
    expect(result.current.error).toBe('');
  });

  // 74ea3c1(삼상태 도입) 회귀 가드: 응답이 오기 전에는 ok가 아니라 unknown이어야 한다.
  // 초기값이 'ok'로 되돌아가면 로딩 중 초록 오탐이 부활한다.
  it('응답이 오기 전에는 ok가 아니라 unknown이다', () => {
    vi.mocked(finUsApi.portfolio).mockReturnValue(new Promise(() => {}));

    const { result } = renderHook(() => useFinUsDashboard());

    expect(result.current.endpointStatuses.portfolio).toBe('unknown');
    expect(result.current.endpointStatuses.health).toBe('unknown');
  });
});
