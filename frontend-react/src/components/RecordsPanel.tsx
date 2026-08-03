import React, { useState } from 'react';
import { BookOpen, ClipboardList, FileText, History } from 'lucide-react';
import type { DashboardResources } from '../types';
import { formatNumber } from '../utils/formatters';

interface RecordsPanelProps {
  resources: DashboardResources;
  loading: boolean;
  onSubmitDiary: (title: string, content: string) => Promise<void>;
}

const RecordsPanel: React.FC<RecordsPanelProps> = ({ resources, loading, onSubmitDiary }) => {
  const [title, setTitle] = useState('');
  const [content, setContent] = useState('');

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    await onSubmitDiary(title, content);
    setTitle('');
    setContent('');
  };

  return (
    <section className="grid grid-cols-1 xl:grid-cols-3 gap-6">
      <div className="bg-white p-6 rounded-lg shadow-xl border border-slate-100">
        <div className="flex items-center gap-3 mb-5">
          <div className="p-2 bg-emerald-50 rounded-lg"><ClipboardList className="w-5 h-5 text-emerald-600" /></div>
          <h3 className="text-slate-900 font-black">Portfolio</h3>
        </div>
        <div className="space-y-3 max-h-72 overflow-auto">
          {resources.portfolio.length === 0 && <p className="text-sm text-slate-400">저장된 포트폴리오가 없습니다.</p>}
          {resources.portfolio.map((item) => (
            <div key={item.id ?? item.stock_code} className="rounded-lg bg-slate-50 p-4">
              <div className="flex justify-between gap-3">
                <div className="font-black text-slate-800">{item.stock_name}</div>
                <div className="text-sm font-bold text-slate-500">{item.stock_code}</div>
              </div>
              <div className="mt-2 grid grid-cols-3 gap-2 text-xs text-slate-500">
                <span>수량 {formatNumber(item.quantity)}</span>
                <span>평단 {formatNumber(item.avg_price)}</span>
                <span>현재 {item.current_price ? formatNumber(item.current_price) : '-'}</span>
              </div>
            </div>
          ))}
        </div>
      </div>

      <div className="bg-white p-6 rounded-lg shadow-xl border border-slate-100">
        <div className="flex items-center gap-3 mb-5">
          <div className="p-2 bg-rose-50 rounded-lg"><History className="w-5 h-5 text-rose-600" /></div>
          <h3 className="text-slate-900 font-black">Trades</h3>
        </div>
        <div className="space-y-3 max-h-72 overflow-auto">
          {resources.trades.length === 0 && <p className="text-sm text-slate-400">저장된 매매 이력이 없습니다.</p>}
          {resources.trades.map((item) => (
            <div key={item.id ?? `${item.stock_code}-${item.trade_date}`} className="rounded-lg bg-slate-50 p-4">
              <div className="flex justify-between gap-3">
                <div className="font-black text-slate-800">{item.stock_name}</div>
                <span className={`text-xs font-black ${item.trade_type === 'BUY' ? 'text-rose-600' : 'text-indigo-600'}`}>
                  {item.trade_type}
                </span>
              </div>
              <div className="mt-2 text-xs text-slate-500">
                {formatNumber(item.quantity)}주 · {formatNumber(item.price)}원
              </div>
            </div>
          ))}
        </div>
      </div>

      <div className="bg-white p-6 rounded-lg shadow-xl border border-slate-100">
        <div className="flex items-center gap-3 mb-5">
          <div className="p-2 bg-indigo-50 rounded-lg"><FileText className="w-5 h-5 text-indigo-600" /></div>
          <h3 className="text-slate-900 font-black">Saved Reports</h3>
        </div>
        <div className="space-y-3 max-h-72 overflow-auto">
          {resources.reports.length === 0 && <p className="text-sm text-slate-400">저장된 리포트가 없습니다.</p>}
          {resources.reports.map((item) => (
            <div key={item.id ?? `${item.stock_name}-${item.created_at}`} className="rounded-lg bg-slate-50 p-4">
              <div className="flex justify-between gap-3">
                <div className="font-black text-slate-800">{item.stock_name}</div>
                <div className="flex items-center gap-2">
                  <span
                    className={`text-[10px] font-black uppercase px-2 py-0.5 rounded-full ${
                      item.provider_supports_tools
                        ? 'bg-emerald-100 text-emerald-700'
                        : 'bg-slate-200 text-slate-500'
                    }`}
                    title={
                      item.provider_supports_tools
                        ? `provider(${item.provider})가 도구 호출 경로로 구성되어 있음 — 실제 호출 여부는 확인되지 않았습니다.`
                        : `provider(${item.provider})는 도구 없이 생성된 리포트입니다.`
                    }
                  >
                    {item.provider_supports_tools ? 'TOOL-CAPABLE' : 'NO TOOLS'}
                  </span>
                  <span className="text-xs font-black text-slate-500">{item.decision}</span>
                </div>
              </div>
              <p className="mt-2 line-clamp-3 text-xs leading-relaxed text-slate-500">{item.summary}</p>
            </div>
          ))}
        </div>
      </div>

      <div className="xl:col-span-3 bg-white p-6 rounded-lg shadow-xl border border-slate-100">
        <div className="flex items-center gap-3 mb-5">
          <div className="p-2 bg-amber-50 rounded-lg"><BookOpen className="w-5 h-5 text-amber-600" /></div>
          <h3 className="text-slate-900 font-black">Diary</h3>
        </div>
        <form onSubmit={submit} className="grid grid-cols-1 lg:grid-cols-[240px_1fr_auto] gap-3 mb-5">
          <input
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            placeholder="일지 제목"
            className="rounded-lg border border-slate-200 px-4 py-3 text-sm focus:outline-none focus:ring-4 focus:ring-amber-100"
          />
          <input
            value={content}
            onChange={(e) => setContent(e.target.value)}
            placeholder="일지 내용"
            className="rounded-lg border border-slate-200 px-4 py-3 text-sm focus:outline-none focus:ring-4 focus:ring-amber-100"
          />
          <button
            type="submit"
            disabled={loading}
            className="rounded-lg bg-amber-500 px-5 py-3 text-sm font-black text-white disabled:bg-slate-300"
          >
            저장
          </button>
        </form>
        <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-3">
          {resources.diaries.length === 0 && <p className="text-sm text-slate-400">저장된 투자 일지가 없습니다.</p>}
          {resources.diaries.slice(0, 6).map((item) => (
            <div key={item.id ?? item.created_at} className="rounded-lg bg-slate-50 p-4">
              <div className="font-black text-slate-800">{item.title}</div>
              <p className="mt-2 text-sm leading-relaxed text-slate-500">{item.content}</p>
            </div>
          ))}
        </div>
      </div>
    </section>
  );
};

export default RecordsPanel;
