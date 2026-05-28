import React, { useState } from 'react';
import { BookOpen, ClipboardList, FileText, History, Loader2, Sparkles, Archive } from 'lucide-react';
import type { DashboardResources, DiaryItem } from '../types';
import { formatNumber } from '../utils/formatters';

interface RecordsPanelProps {
  resources: DashboardResources;
  loading: boolean;
  diaryAiLoading: boolean;
  diaryListLoading: boolean;
  diarySaveLoading: boolean;
  showAllDiaries: boolean;
  diaryAgentReport: string;
  onSubmitDiary: (title: string, content: string) => Promise<void>;
  onLoadPastDiaries: () => Promise<void>;
  onGenerateDiaryWithAi: () => Promise<{ title: string; content: string } | null>;
}

function formatDiaryDate(value?: string) {
  if (!value) return '';
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value;
  return parsed.toLocaleString('ko-KR', {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  });
}

const RecordsPanel: React.FC<RecordsPanelProps> = ({
  resources,
  loading,
  diaryAiLoading,
  diaryListLoading,
  diarySaveLoading,
  showAllDiaries,
  diaryAgentReport,
  onSubmitDiary,
  onLoadPastDiaries,
  onGenerateDiaryWithAi,
}) => {
  const [title, setTitle] = useState('');
  const [content, setContent] = useState('');
  const [selectedDiaryId, setSelectedDiaryId] = useState<number | undefined>();

  const diaryBusy = diaryAiLoading || diaryListLoading || diarySaveLoading;
  const busy = loading || diaryBusy;
  const visibleDiaries = showAllDiaries ? resources.diaries : resources.diaries.slice(0, 6);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    await onSubmitDiary(title, content);
    setTitle('');
    setContent('');
    setSelectedDiaryId(undefined);
  };

  const handleGenerateAi = async () => {
    const draft = await onGenerateDiaryWithAi();
    if (draft) {
      setTitle(draft.title);
      setContent(draft.content);
      // AI 초안은 아직 DB 에 저장되지 않은 새 글이므로 과거 일지 카드 선택을 해제한다.
      setSelectedDiaryId(undefined);
    }
  };

  const handleLoadPast = async () => {
    await onLoadPastDiaries();
  };

  const selectDiary = (item: DiaryItem) => {
    setTitle(item.title);
    setContent(item.content);
    setSelectedDiaryId(item.id);
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
                <span className="text-xs font-black text-slate-500">{item.decision}</span>
              </div>
              <p className="mt-2 line-clamp-3 text-xs leading-relaxed text-slate-500">{item.summary}</p>
            </div>
          ))}
        </div>
      </div>

      <div className="xl:col-span-3 bg-white p-6 rounded-lg shadow-xl border border-slate-100">
        <div className="flex flex-wrap items-center justify-between gap-3 mb-5">
          <div className="flex items-center gap-3">
            <div className="p-2 bg-amber-50 rounded-lg"><BookOpen className="w-5 h-5 text-amber-600" /></div>
            <h3 className="text-slate-900 font-black">Diary</h3>
          </div>
          <div className="flex flex-wrap gap-2">
            <button
              type="button"
              onClick={handleGenerateAi}
              disabled={busy}
              className="inline-flex items-center gap-2 rounded-lg bg-violet-600 px-4 py-2.5 text-sm font-black text-white disabled:bg-slate-300"
            >
              {diaryAiLoading ? <Loader2 className="w-4 h-4 animate-spin" /> : <Sparkles className="w-4 h-4" />}
              AI로 작성하기
            </button>
            <button
              type="button"
              onClick={handleLoadPast}
              disabled={busy}
              className="inline-flex items-center gap-2 rounded-lg border border-amber-300 bg-amber-50 px-4 py-2.5 text-sm font-black text-amber-900 disabled:opacity-50"
            >
              {diaryListLoading ? <Loader2 className="w-4 h-4 animate-spin" /> : <Archive className="w-4 h-4" />}
              과거 매매일지 가져오기
            </button>
          </div>
        </div>

        {diaryAgentReport && (
          <div className="mb-4 rounded-lg border border-violet-100 bg-violet-50/80 p-4">
            <p className="text-xs font-black uppercase tracking-wide text-violet-700">AI 초안 (미저장)</p>
            <p className="mt-1 text-xs text-violet-600">내용을 확인·수정한 뒤 아래 「저장」 버튼을 눌러 backend에 저장하세요.</p>
            <p className="mt-2 max-h-40 overflow-auto whitespace-pre-wrap text-sm leading-relaxed text-slate-700">
              {diaryAgentReport}
            </p>
          </div>
        )}

        <form onSubmit={submit} className="grid grid-cols-1 lg:grid-cols-[240px_1fr_auto] gap-3 mb-5">
          <input
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            placeholder="일지 제목"
            className="rounded-lg border border-slate-200 px-4 py-3 text-sm focus:outline-none focus:ring-4 focus:ring-amber-100"
          />
          <textarea
            value={content}
            onChange={(e) => setContent(e.target.value)}
            placeholder="일지 내용"
            rows={3}
            className="min-h-[88px] resize-y rounded-lg border border-slate-200 px-4 py-3 text-sm focus:outline-none focus:ring-4 focus:ring-amber-100"
          />
          <button
            type="submit"
            disabled={busy}
            className="rounded-lg bg-amber-500 px-5 py-3 text-sm font-black text-white disabled:bg-slate-300 lg:self-start"
          >
            {diarySaveLoading ? '저장 중…' : '저장'}
          </button>
        </form>

        <p className="mb-3 text-xs text-slate-500">
          {showAllDiaries
            ? `저장된 일지 ${resources.diaries.length}건 · 카드를 클릭하면 편집란에 불러옵니다.`
            : `최근 ${visibleDiaries.length}건 미리보기 · 전체 목록은 「과거 매매일지 가져오기」를 누르세요.`}
        </p>

        <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-3 max-h-[28rem] overflow-auto">
          {visibleDiaries.length === 0 && (
            <p className="text-sm text-slate-400 md:col-span-2 xl:col-span-3">저장된 투자 일지가 없습니다.</p>
          )}
          {visibleDiaries.map((item) => {
            const isSelected = selectedDiaryId !== undefined && item.id === selectedDiaryId;
            return (
              <button
                key={item.id ?? item.created_at}
                type="button"
                onClick={() => selectDiary(item)}
                className={`rounded-lg p-4 text-left transition ${
                  isSelected
                    ? 'bg-amber-100 ring-2 ring-amber-400'
                    : 'bg-slate-50 hover:bg-amber-50/60'
                }`}
              >
                <div className="font-black text-slate-800">{item.title}</div>
                {item.created_at && (
                  <div className="mt-1 text-xs font-bold text-slate-400">{formatDiaryDate(item.created_at)}</div>
                )}
                <p className="mt-2 line-clamp-4 text-sm leading-relaxed text-slate-500">{item.content}</p>
              </button>
            );
          })}
        </div>
      </div>
    </section>
  );
};

export default RecordsPanel;
