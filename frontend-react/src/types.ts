export interface TradingSignal {
  decision: 'BUY' | 'SELL' | 'HOLD';
  confidence_score: number;
  reason: string;
  target_stock: string;
}

export interface AnalysisReport {
  summary: string;
  // 백엔드 schemas.py는 details: TradingSignal(필수)이지만, /api/v1/analyze의
  // response_model이 CommonResponse(data: Any | None)라 이 필드가 스키마 검증을
  // 거치지 않는다. 방어적으로 nullable로 선언한다 (#70).
  details: TradingSignal | null;
  source_news: string[];
  source_signals?: string[];
  trading_trend: string | null;
  // provider가 도구(MCP/KIS/뉴스)를 호출할 수 있는 경로로 구성돼 있는지 여부.
  // provider 자체에서 파생된 "능력" 신호일 뿐, 이 응답에서 실제로 도구가
  // 호출됐다는 관측은 아니다 (백엔드 #152 참고).
  // 백엔드 schemas.py의 provider: str | None = None과 일치시킨다.
  provider: string | null;
  provider_supports_tools: boolean;
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

export interface ApiResponse<T> {
  status: string;
  // 백엔드 schemas.py의 CommonResponse.data: Any | None = None과 일치시킨다.
  // api.ts의 unwrap()이 런타임에서 null을 걸러 T를 반환하므로, 이 필드를
  // 우회해 response.data.data를 직접 쓰는 코드는 컴파일 타임에 걸린다 (#70).
  data: T | null;
  message?: string | null;
}

export interface HealthStatus {
  status: string;
  nat_base_url: string;
}

export interface PortfolioItem {
  id?: number;
  stock_code: string;
  stock_name: string;
  quantity: number;
  avg_price: number;
  current_price?: number | null;
  updated_at: string;
}

export interface TradeHistoryItem {
  id?: number;
  stock_code: string;
  stock_name: string;
  trade_type: string;
  quantity: number;
  price: number;
  trade_date: string;
}

export interface AgentReportItem {
  id?: number;
  stock_code: string;
  stock_name: string;
  // 백엔드 models.py의 provider: str과 실제 API 직렬화 상 null 가능성에 맞춰
  // string | null로 선언한다. UI 보간 시 ?? 폴백을 사용한다.
  provider: string | null;
  summary: string;
  decision: string;
  confidence_score: number;
  reason: string;
  // provider가 도구(MCP/KIS/뉴스)를 호출할 수 있는 경로로 구성돼 있는지 여부.
  // provider 자체에서 파생된 "능력" 신호일 뿐, 이 리포트에서 실제로 도구가
  // 호출됐다는 관측은 아니다 (백엔드 #152 참고).
  provider_supports_tools: boolean;
  created_at: string;
}

export interface DiaryItem {
  id?: number;
  title: string;
  content: string;
  created_at: string;
}

export interface AccountBalance {
  report: string;
}

export interface DashboardResources {
  health: HealthStatus | null;
  balance: AccountBalance | null;
  portfolio: PortfolioItem[];
  trades: TradeHistoryItem[];
  reports: AgentReportItem[];
  diaries: DiaryItem[];
}

export type EndpointStatus = 'unknown' | 'ok' | 'fail';

export interface EndpointStatuses {
  health: EndpointStatus;
  balance: EndpointStatus;
  portfolio: EndpointStatus;
  trades: EndpointStatus;
  reports: EndpointStatus;
  diaries: EndpointStatus;
}

export interface ChatMessage {
  id: string;
  role: 'system' | 'user' | 'server';
  text: string;
  createdAt: string;
}
