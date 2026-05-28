import React from 'react';
import { Activity, BarChart3, Layers } from 'lucide-react';
import {
  BarChart,
  Bar,
  AreaChart,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  ResponsiveContainer,
  ReferenceLine,
  Cell,
  Area,
} from 'recharts';
import { AnalysisReport, TrendItem } from '../types';
import { formatNumber } from '../utils/formatters';

interface MainDashboardProps {
  trendData: TrendItem[];
  report: AnalysisReport | null;
}

const MainDashboard: React.FC<MainDashboardProps> = ({ trendData, report }) => {
  return (
    <div className="xl:col-span-3 space-y-8">
      <div className="grid grid-cols-1 md:grid-cols-2 gap-8">
        <div className="bg-white p-8 rounded-[2.5rem] shadow-2xl border border-slate-50">
          <div className="flex items-center gap-3 mb-8">
            <div className="p-2 bg-rose-50 rounded-lg"><Activity className="w-6 h-6 text-rose-500" /></div>
            <h3 className="text-slate-800 font-black text-xl tracking-tight">Price Trend</h3>
          </div>
          <div className="h-64 min-h-64 min-w-0 w-full">
            <ResponsiveContainer width="100%" height="100%" minWidth={0}>
              <AreaChart data={trendData}>
                <Area type="monotone" dataKey="price" fill="#fff1f2" stroke="#fb7185" strokeWidth={4} />
              </AreaChart>
            </ResponsiveContainer>
          </div>
        </div>
        <div className="bg-white p-8 rounded-[2.5rem] shadow-2xl border border-slate-50">
          <div className="flex items-center gap-3 mb-8">
            <div className="p-2 bg-slate-50 rounded-lg"><BarChart3 className="w-6 h-6 text-slate-500" /></div>
            <h3 className="text-slate-800 font-black text-xl tracking-tight">Liquidity</h3>
          </div>
          <div className="h-64 min-h-64 min-w-0 w-full">
            <ResponsiveContainer width="100%" height="100%" minWidth={0}>
              <BarChart data={trendData}>
                <Bar dataKey="volume" fill="#f1f5f9" radius={[4, 4, 0, 0]} />
              </BarChart>
            </ResponsiveContainer>
          </div>
        </div>
      </div>

      <div className="bg-white p-10 rounded-[2.5rem] shadow-2xl border border-slate-50">
        <div className="flex items-center justify-between mb-8">
          <div className="flex items-center gap-3">
            <div className="p-2 bg-indigo-50 rounded-lg"><Layers className="w-6 h-6 text-indigo-500" /></div>
            <h3 className="text-slate-800 font-black text-xl tracking-tight">Investor Flow</h3>
          </div>
          <div className="flex gap-4">
            <div className="flex items-center gap-2"><div className="w-4 h-1.5 rounded-full bg-rose-400" /><span className="text-[10px] font-black text-slate-400 uppercase">Foreigner</span></div>
            <div className="flex items-center gap-2"><div className="w-4 h-1.5 rounded-full bg-indigo-400" /><span className="text-[10px] font-black text-slate-400 uppercase">Institutional</span></div>
          </div>
        </div>

        <div className="h-64 min-h-64 min-w-0 w-full">
          <ResponsiveContainer width="100%" height="100%" minWidth={0}>
            <BarChart data={trendData} margin={{ top: 0, right: 0, left: 0, bottom: 0 }}>
              <CartesianGrid strokeDasharray="3 3" vertical={false} stroke="#f1f5f9" />
              <XAxis dataKey="date" axisLine={false} tickLine={false} tick={{ fontSize: 12, fontWeight: 700, fill: '#cbd5e1' }} />
              <YAxis hide />
              <Tooltip
                cursor={{ fill: '#f8fafc' }}
                contentStyle={{ borderRadius: '24px', border: 'none', boxShadow: '0 25px 50px -12px rgb(0 0 0 / 0.15)', padding: '20px' }}
                formatter={(value) => [formatNumber(Number(value)), '']}
              />
              <ReferenceLine y={0} stroke="#cbd5e1" strokeWidth={2} />
              <Bar dataKey="foreigner" radius={[6, 6, 0, 0]}>
                {trendData.map((entry, index) => (
                  <Cell key={`cell-f-${index}`} fill={entry.foreigner > 0 ? '#fb7185' : '#fda4af'} />
                ))}
              </Bar>
              <Bar dataKey="institution" radius={[6, 6, 0, 0]}>
                {trendData.map((entry, index) => (
                  <Cell key={`cell-i-${index}`} fill={entry.institution > 0 ? '#6366f1' : '#a5b4fc'} />
                ))}
              </Bar>
            </BarChart>
          </ResponsiveContainer>
        </div>

        {report && (
          <div className="mt-10 p-8 bg-slate-900 rounded-3xl shadow-indigo-100 shadow-2xl relative overflow-hidden group">
            <div className="absolute top-0 right-0 p-4 opacity-10 group-hover:opacity-20 transition-opacity">
              <Activity className="w-24 h-24 text-white" />
            </div>
            <h4 className="text-[10px] font-black text-indigo-400 mb-3 uppercase tracking-[0.3em]">Neural Synthesis</h4>
            <p className="text-white text-lg leading-relaxed font-bold tracking-tight">"{report.summary}"</p>
          </div>
        )}
      </div>
    </div>
  );
};

export default MainDashboard;
