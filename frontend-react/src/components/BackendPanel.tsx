import React from 'react';
import { Activity, RefreshCw, Server, WalletCards } from 'lucide-react';
import type { DashboardResources, EndpointStatus, EndpointStatuses } from '../types';

interface BackendPanelProps {
  resources: DashboardResources;
  endpointStatuses: EndpointStatuses;
  loading: boolean;
  onRefresh: () => void;
}

const DOT_CLASS: Record<EndpointStatus, string> = {
  ok: 'bg-emerald-500',
  fail: 'bg-rose-500',
  unknown: 'bg-slate-300',
};

// 상태 점은 색상만으로 정보를 전달하므로 스크린리더에는 아무것도 읽히지 않는다.
// aria-label로 상태를 텍스트화하고, data-status로 색상과 무관한 검증 지점을 남긴다.
const STATUS_LABEL: Record<EndpointStatus, string> = {
  ok: '정상',
  fail: '실패',
  unknown: '확인 중',
};

// 상태 점에 role="img"를 쓰는 이유: role="status"는 암묵적 aria-live="polite"를 동반해
// 엔드포인트 6개마다 라이브 리전이 생긴다. 이 점은 변경 공지가 아니라 현재 상태 지표이므로
// role="img"가 의미에 맞다. 회귀 가드는 BackendPanel.test.tsx의 getByRole('img', ...) 참조.

const BackendPanel: React.FC<BackendPanelProps> = ({ resources, endpointStatuses, loading, onRefresh }) => {
  const endpoints = [
    { label: 'Health', status: endpointStatuses.health, path: '/health' },
    { label: 'Balance', status: endpointStatuses.balance, path: '/api/v1/trading/balance' },
    { label: 'Portfolio', status: endpointStatuses.portfolio, path: '/api/v1/db/portfolio' },
    { label: 'Trades', status: endpointStatuses.trades, path: '/api/v1/db/trades' },
    { label: 'Reports', status: endpointStatuses.reports, path: '/api/v1/db/reports' },
    { label: 'Diary', status: endpointStatuses.diaries, path: '/api/v1/db/diary' },
  ];

  return (
    <section className="grid grid-cols-1 lg:grid-cols-3 gap-6">
      <div className="bg-white p-6 rounded-lg shadow-xl border border-slate-100">
        <div className="flex items-center justify-between mb-5">
          <div className="flex items-center gap-3">
            <div className="p-2 bg-emerald-50 rounded-lg"><Server className="w-5 h-5 text-emerald-600" /></div>
            <h3 className="text-slate-900 font-black">Backend</h3>
          </div>
          <button
            type="button"
            onClick={onRefresh}
            className="p-2 rounded-lg bg-slate-100 hover:bg-slate-200 text-slate-600"
            title="백엔드 상태 새로고침"
          >
            <RefreshCw className={`w-4 h-4 ${loading ? 'animate-spin' : ''}`} />
          </button>
        </div>
        <div className="space-y-3">
          <div className="text-sm text-slate-500">
            <span className="font-bold text-slate-700">Status</span>: {resources.health?.status || 'unknown'}
          </div>
          <div className="text-sm text-slate-500 break-all">
            <span className="font-bold text-slate-700">NAT</span>: {resources.health?.nat_base_url || '-'}
          </div>
        </div>
      </div>

      <div className="bg-white p-6 rounded-lg shadow-xl border border-slate-100">
        <div className="flex items-center gap-3 mb-5">
          <div className="p-2 bg-indigo-50 rounded-lg"><Activity className="w-5 h-5 text-indigo-600" /></div>
          <h3 className="text-slate-900 font-black">Endpoints</h3>
        </div>
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
          {endpoints.map((endpoint) => (
            <div key={endpoint.path} className="flex items-center gap-2 rounded-lg bg-slate-50 px-3 py-2">
              <span
                role="img"
                aria-label={`${endpoint.label} ${STATUS_LABEL[endpoint.status]}`}
                data-status={endpoint.status}
                className={`w-2.5 h-2.5 rounded-full ${DOT_CLASS[endpoint.status]}`}
              />
              <div className="min-w-0">
                <div className="text-xs font-black text-slate-700">{endpoint.label}</div>
                <div className="text-[11px] text-slate-400 truncate">{endpoint.path}</div>
              </div>
            </div>
          ))}
        </div>
      </div>

      <div className="bg-white p-6 rounded-lg shadow-xl border border-slate-100">
        <div className="flex items-center gap-3 mb-5">
          <div className="p-2 bg-amber-50 rounded-lg"><WalletCards className="w-5 h-5 text-amber-600" /></div>
          <h3 className="text-slate-900 font-black">Account Balance</h3>
        </div>
        <pre className="max-h-40 overflow-auto whitespace-pre-wrap text-xs leading-relaxed text-slate-600 bg-slate-50 rounded-lg p-4">
          {resources.balance?.report || '잔고 응답이 아직 없습니다.'}
        </pre>
      </div>
    </section>
  );
};

export default BackendPanel;
