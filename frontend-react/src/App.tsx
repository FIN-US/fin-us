import React from 'react';
import { parseTrendData } from './utils/parsers';
import Header from './components/Header';
import SearchForm from './components/SearchForm';
import ErrorDisplay from './components/ErrorDisplay';
import StockHeader from './components/StockHeader';
import ReportSidebar from './components/ReportSidebar';
import MainDashboard from './components/MainDashboard';
import EmptyState from './components/EmptyState';
import { useFinUsDashboard } from './hooks/useFinUsDashboard';

export default function App() {
  const {
    stock,
    setStock,
    provider,
    setProvider,
    loading,
    report,
    rawNews,
    rawTrend,
    error,
    handleAnalyze,
    handleFetchData,
  } = useFinUsDashboard();

  const currentTrend = report?.trading_trend || rawTrend;
  const currentNews = report?.source_signals || report?.source_news || rawNews;
  const trendData = parseTrendData(currentTrend);
  const latest = trendData[trendData.length - 1];

  const hasData = report || currentNews.length > 0 || currentTrend;

  return (
    <div className="max-w-7xl mx-auto px-4 py-8 bg-slate-50 min-h-screen">
      <Header />

      <SearchForm
        stock={stock}
        setStock={setStock}
        provider={provider}
        setProvider={setProvider}
        loading={loading}
        handleFetchData={handleFetchData}
        handleAnalyze={handleAnalyze}
      />

      <ErrorDisplay error={error} />

      {latest && <StockHeader stock={stock} latest={latest} />}

      {hasData ? (
        <div className="grid grid-cols-1 xl:grid-cols-4 gap-8">
          <ReportSidebar report={report} currentNews={currentNews} />
          <MainDashboard trendData={trendData} report={report} />
        </div>
      ) : (
        <EmptyState loading={loading} />
      )}
    </div>
  );
}
