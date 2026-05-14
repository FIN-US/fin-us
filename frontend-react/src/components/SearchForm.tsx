import React from 'react';
import { Search, Loader2 } from 'lucide-react';

interface SearchFormProps {
  stock: string;
  setStock: (value: string) => void;
  provider: string;
  setProvider: (value: string) => void;
  loading: boolean;
  handleFetchData: (e: React.FormEvent) => void;
  handleAnalyze: (e: React.FormEvent) => void;
}

const SearchForm: React.FC<SearchFormProps> = ({
  stock,
  setStock,
  provider,
  setProvider,
  loading,
  handleFetchData,
  handleAnalyze,
}) => {
  return (
    <div className="mb-12">
      <form className="flex flex-col md:flex-row gap-4 items-center justify-center">
        <div className="relative w-full max-w-lg">
          <Search className="absolute left-4 top-1/2 -translate-y-1/2 text-slate-300 w-6 h-6" />
          <input
            type="text"
            value={stock}
            onChange={(e) => setStock(e.target.value)}
            placeholder="분석할 종목명 (예: 삼성전자)"
            className="w-full pl-12 pr-4 py-4 rounded-2xl border-none focus:ring-4 focus:ring-indigo-100 focus:outline-none transition-all shadow-xl font-medium text-lg"
          />
        </div>

        <div className="flex flex-col sm:flex-row gap-3 w-full md:w-auto">
          <button
            onClick={handleFetchData}
            disabled={loading}
            className="bg-white hover:bg-slate-50 text-slate-600 px-6 py-4 rounded-lg font-bold transition-all shadow-lg border border-slate-100"
          >
            데이터 조회
          </button>

          <div className="flex items-center gap-2 bg-indigo-600 px-3 py-2 rounded-lg shadow-indigo-200 shadow-xl">
            <select
              value={provider}
              onChange={(e) => setProvider(e.target.value)}
              className="focus:outline-none bg-transparent text-xs font-black text-white mr-2 px-2"
            >
              <option value="openai" className="text-slate-900">OpenAI API</option>
              <option value="anthropic" className="text-slate-900">Anthropic API</option>
              <option value="ollama" className="text-slate-900">Ollama</option>
              <option value="nat" className="text-slate-900">NAT</option>
            </select>
            <button
              onClick={handleAnalyze}
              disabled={loading}
              className="bg-white hover:bg-slate-100 disabled:bg-slate-300 text-indigo-600 px-6 py-2 rounded-md font-black transition-all"
            >
              {loading ? <Loader2 className="w-5 h-5 animate-spin" /> : '분석'}
            </button>
          </div>
        </div>
      </form>
    </div>
  );
};

export default SearchForm;
