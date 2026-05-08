export interface TradingSignal {
  decision: 'BUY' | 'SELL' | 'HOLD';
  confidence_score: number;
  reason: string;
  target_stock: string;
}

export interface AnalysisReport {
  summary: string;
  details: TradingSignal;
  source_news: string[];
  source_signals?: string[];
  trading_trend: string | null;
}

export interface TrendItem {
  date: string;
  price: number;
  changeVal: string;
  changePct: string;
  isUp: boolean;
  foreigner: number;
  institution: number;
  volume: number;
}
