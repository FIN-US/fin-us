import { describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import BackendPanel from './BackendPanel';
import type { DashboardResources } from '../types';
import type { EndpointOk } from '../hooks/useFinUsDashboard';

// #71 회귀 가드: fetch에 실패한 엔드포인트는 초록(ok) 표시가 되면 안 된다.
// 당시 버그는 `resources.portfolio.length >= 0` 같은 항상-참 술어를 상태 판정에
// 사용해서, 실패해도 항상 ok로 보였다. 이 테스트는 endpointOk 값을 직접 신뢰원으로
// 사용해 상태 점 색상을 검증한다.

const baseResources: DashboardResources = {
  health: null,
  balance: null,
  portfolio: [],
  trades: [],
  reports: [],
  diaries: [],
};

function findDot(label: string) {
  const labelEl = screen.getByText(label);
  const row = labelEl.closest('.bg-slate-50');
  return row?.querySelector('span');
}

describe('BackendPanel', () => {
  it('marks a failed endpoint as red, not green', () => {
    const endpointOk: EndpointOk = {
      health: 'ok',
      balance: 'ok',
      portfolio: 'fail',
      trades: 'ok',
      reports: 'ok',
      diaries: 'ok',
    };

    render(
      <BackendPanel
        resources={baseResources}
        endpointOk={endpointOk}
        loading={false}
        onRefresh={vi.fn()}
      />,
    );

    const portfolioDot = findDot('Portfolio');
    expect(portfolioDot).toHaveClass('bg-rose-500');
    expect(portfolioDot).not.toHaveClass('bg-emerald-500');
  });

  it('marks a successful endpoint as green, not red', () => {
    const endpointOk: EndpointOk = {
      health: 'ok',
      balance: 'ok',
      portfolio: 'ok',
      trades: 'ok',
      reports: 'ok',
      diaries: 'ok',
    };

    render(
      <BackendPanel
        resources={baseResources}
        endpointOk={endpointOk}
        loading={false}
        onRefresh={vi.fn()}
      />,
    );

    const portfolioDot = findDot('Portfolio');
    expect(portfolioDot).toHaveClass('bg-emerald-500');
    expect(portfolioDot).not.toHaveClass('bg-rose-500');
  });
});
