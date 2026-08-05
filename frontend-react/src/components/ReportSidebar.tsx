import React from 'react';
import { TrendingUp, TrendingDown, Minus, Newspaper } from 'lucide-react';
import { AnalysisReport } from '../types';

interface ReportSidebarProps {
  report: AnalysisReport | null;
  currentNews: string[];
}

const ReportSidebar: React.FC<ReportSidebarProps> = ({ report, currentNews }) => {
  return (
    <div className="xl:col-span-1 space-y-6">
      {report && (
        report.details == null ? (
          <div className="p-8 rounded-[2rem] border-none shadow-2xl bg-gradient-to-br from-slate-400 to-slate-500 text-white">
            <span className="text-xs font-black uppercase tracking-widest opacity-80">AI STRATEGY</span>
            <p className="mt-4 text-sm font-bold opacity-80">분석 결과 없음</p>
          </div>
        ) : (
        <div className={`p-8 rounded-[2rem] border-none shadow-2xl relative overflow-hidden ${
          report.details.decision === 'BUY' ? 'bg-gradient-to-br from-rose-500 to-rose-600 text-white' :
          report.details.decision === 'SELL' ? 'bg-gradient-to-br from-indigo-500 to-indigo-600 text-white' :
          'bg-gradient-to-br from-slate-500 to-slate-600 text-white'
        }`}>
          <div className="flex justify-between items-center mb-6">
            <div className="flex items-center gap-2">
              <span className="text-xs font-black uppercase tracking-widest opacity-80">AI STRATEGY</span>
              <span
                className={`text-[10px] font-black uppercase tracking-wide px-2 py-0.5 rounded-full ${
                  report.provider_supports_tools ? 'bg-white/25' : 'bg-black/20 opacity-80'
                }`}
                title={
                  report.provider_supports_tools
                    ? `provider(${report.provider ?? '알 수 없음'})가 도구(MCP/KIS/뉴스) 호출 경로로 구성되어 있다는 뜻입니다. 이번 응답에서 실제로 도구가 호출되었다는 관측은 아닙니다.`
                    : `provider(${report.provider ?? '알 수 없음'})는 도구(MCP/KIS/뉴스) 없이 모델이 직접 답을 생성했습니다. 수치가 지어낸 값일 수 있습니다.`
                }
              >
                {report.provider_supports_tools ? 'Tool-capable (unconfirmed)' : 'No tool access'}
              </span>
            </div>
            {report.details.decision === 'BUY' ? <TrendingUp className="w-8 h-8 opacity-50" /> :
             report.details.decision === 'SELL' ? <TrendingDown className="w-8 h-8 opacity-50" /> : <Minus className="w-8 h-8 opacity-50" />}
          </div>
          <h2 className="text-7xl font-black mb-4 tracking-tighter">{report.details.decision}</h2>
          <div className="space-y-2 mb-8">
            <div className="flex justify-between text-xs font-black opacity-80 uppercase">
              <span>Confidence</span>
              <span>{(report.details.confidence_score * 100).toFixed(0)}%</span>
            </div>
            <div className="h-3 w-full bg-white/20 rounded-full overflow-hidden">
              <div className="h-full bg-white transition-all shadow-[0_0_10px_rgba(255,255,255,0.5)]" style={{ width: `${report.details.confidence_score * 100}%` }} />
            </div>
          </div>
          <p className="text-sm leading-relaxed font-bold bg-black/10 p-5 rounded-2xl backdrop-blur-sm">
            {report.details.reason}
          </p>
        </div>
        )
      )}

      <div className="bg-white p-6 rounded-[2rem] shadow-xl border border-slate-50">
        <div className="flex items-center gap-3 mb-6">
          <div className="p-2 bg-indigo-50 rounded-lg"><Newspaper className="w-5 h-5 text-indigo-600" /></div>
          <h3 className="text-slate-800 font-black tracking-tight text-lg">Market Context</h3>
        </div>
        <ul className="space-y-4">
          {currentNews.map((news, i) => (
            <li key={i} className="text-sm text-slate-500 font-medium leading-snug hover:text-indigo-600 transition-colors cursor-default border-b border-slate-50 pb-3 last:border-0">
              {news}
            </li>
          ))}
        </ul>
      </div>
    </div>
  );
};

export default ReportSidebar;
