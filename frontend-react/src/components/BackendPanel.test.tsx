import { describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import BackendPanel from './BackendPanel';
import type { DashboardResources, EndpointStatus, EndpointStatuses } from '../types';

// 렌더링 매핑 가드: 훅이 판정한 EndpointStatus가 화면의 상태 점에 그대로 반영되는지 본다.
// 판정 로직 자체의 #71 회귀 가드는 useFinUsDashboard.test.tsx가 담당한다.
//
// 검증 지점을 색상 클래스가 아니라 data-status/aria-label로 잡는 이유:
// Tailwind 팔레트를 rose -> red로 바꿔도 테스트는 안 깨지고, 상태 판정이 틀리면 반드시 깨진다.

const baseResources: DashboardResources = {
  health: null,
  balance: null,
  portfolio: [],
  trades: [],
  reports: [],
  diaries: [],
};

const allOk: EndpointStatuses = {
  health: 'ok',
  balance: 'ok',
  portfolio: 'ok',
  trades: 'ok',
  reports: 'ok',
  diaries: 'ok',
};

// [상태, 기대 클래스, 나오면 안 되는 클래스들]
// unknown 케이스는 74ea3c1(로딩 중 초록 오탐 제거)의 회귀 가드다.
// unknown이 다시 bg-emerald-500으로 돌아가면 여기서 red가 된다.
const CASES: Array<[EndpointStatus, string, string, string[]]> = [
  ['fail', '실패', 'bg-rose-500', ['bg-emerald-500', 'bg-slate-300']],
  ['ok', '정상', 'bg-emerald-500', ['bg-rose-500', 'bg-slate-300']],
  ['unknown', '확인 중', 'bg-slate-300', ['bg-emerald-500', 'bg-rose-500']],
];

describe('BackendPanel', () => {
  it.each(CASES)(
    'portfolio=%s 이면 상태 점이 "%s"(%s)로 표시된다',
    (status, statusLabel, expected, forbidden) => {
      render(
        <BackendPanel
          resources={baseResources}
          endpointStatuses={{ ...allOk, portfolio: status }}
          loading={false}
          onRefresh={vi.fn()}
        />,
      );

      const dot = screen.getByLabelText(`Portfolio ${statusLabel}`);
      expect(dot).toHaveAttribute('data-status', status);
      expect(dot).toHaveClass(expected);
      forbidden.forEach((cls) => expect(dot).not.toHaveClass(cls));
    },
  );

  it('엔드포인트마다 독립적인 상태를 표시한다', () => {
    render(
      <BackendPanel
        resources={baseResources}
        endpointStatuses={{ ...allOk, portfolio: 'fail', trades: 'unknown' }}
        loading={false}
        onRefresh={vi.fn()}
      />,
    );

    expect(screen.getByLabelText(/^Portfolio /)).toHaveAttribute('data-status', 'fail');
    expect(screen.getByLabelText(/^Trades /)).toHaveAttribute('data-status', 'unknown');
    expect(screen.getByLabelText(/^Health /)).toHaveAttribute('data-status', 'ok');
  });
});
