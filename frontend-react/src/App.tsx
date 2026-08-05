import { parseTrendData } from './utils/parsers';
import Header from './components/Header';
import SearchForm from './components/SearchForm';
import ErrorDisplay from './components/ErrorDisplay';
import StockHeader from './components/StockHeader';
import ReportSidebar from './components/ReportSidebar';
import MainDashboard from './components/MainDashboard';
import EmptyState from './components/EmptyState';
import BackendPanel from './components/BackendPanel';
import ChatPanel from './components/ChatPanel';
import RecordsPanel from './components/RecordsPanel';
import { useFinUsDashboard } from './hooks/useFinUsDashboard';

export default function App() {
  const {
    stock,
    setStock,
    provider,
    setProvider,
    loading,
    resourceLoading,
    report,
    rawNews,
    rawTrend,
    resources,
    endpointOk,
    error,
    chatStatus,
    chatMessages,
    handleAnalyze,
    handleFetchData,
    loadResources,
    submitDiary,
    sendChatMessage,
  } = useFinUsDashboard();

  const currentTrend = report?.trading_trend || rawTrend;
  const currentNews = report?.source_signals || report?.source_news || rawNews;
  const trendData = parseTrendData(currentTrend);
  const latest = trendData[trendData.length - 1];
  const hasData = report || currentNews.length > 0 || currentTrend;

  return (
    <div className="min-h-screen bg-slate-50">
      <main className="max-w-7xl mx-auto px-4 py-8 space-y-8">
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

        <BackendPanel resources={resources} endpointOk={endpointOk} loading={resourceLoading} onRefresh={loadResources} />

        {latest && <StockHeader stock={stock} latest={latest} />}

        {hasData ? (
          <div className="grid grid-cols-1 xl:grid-cols-4 gap-8">
            <ReportSidebar report={report} currentNews={currentNews} />
            <MainDashboard trendData={trendData} report={report} />
          </div>
        ) : (
          <EmptyState loading={loading} />
        )}

        <div className="grid grid-cols-1 xl:grid-cols-[1fr_420px] gap-6">
          <RecordsPanel resources={resources} loading={resourceLoading} onSubmitDiary={submitDiary} />
          <ChatPanel status={chatStatus} messages={chatMessages} onSend={sendChatMessage} />
        </div>
      </main>
    </div>
  );
}
