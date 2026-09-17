// ─── Order Book ───────────────────────────────────────────────────────────────
export interface OrderBookLevel {
  price: number;
  size: number;
  total: number;
}

export interface OrderBook {
  ticker: string;
  bids: OrderBookLevel[];
  asks: OrderBookLevel[];
  spread: number;
  mid: number;
  timestamp: string;
}

// ─── Risk ─────────────────────────────────────────────────────────────────────
export interface RiskMetric {
  label: string;
  current: number;
  limit: number;
  unit: string;
  pct: number;
  status: 'ok' | 'warn' | 'breach';
}

export interface RiskState {
  metrics: RiskMetric[];
  overall: 'PASS' | 'WARN' | 'BREACH';
}

// ─── PnL ──────────────────────────────────────────────────────────────────────
export interface PnLPoint {
  time: string;
  value: number;
}

export interface PnLState {
  session: number;
  unrealised: number;
  realised: number;
  cash: number;
  equity: number;
  history: PnLPoint[];
}

// ─── Fills ────────────────────────────────────────────────────────────────────
export type Side = 'buy' | 'sell';

export interface Fill {
  id: string;
  side: Side;
  ticker: string;
  qty: number;
  price: number;
  timestamp: string;
  source: string;
}

// ─── ZMQ Signals ──────────────────────────────────────────────────────────────
export interface Signal {
  ticker: string;
  value: number;
  direction: 1 | -1;
  confidence: number;
  strategy: string;
  received_at: string;
}

export interface ZmqState {
  signals: Signal[];
  latency_ms: number;
  socket_status: 'connected' | 'reconnecting' | 'disconnected';
  messages_recv: number;
}

// ─── Circuit Breaker ──────────────────────────────────────────────────────────
export type BreakerState = 'open' | 'half' | 'tripped';

export interface CircuitBreaker {
  name: string;
  state: BreakerState;
  reason: string;
  triggered_at?: string;
}

// ─── Alpaca Gateway ───────────────────────────────────────────────────────────
export interface GatewayState {
  connected: boolean;
  stream_active: boolean;
  symbols_subscribed: number;
  last_tick_ms: number;
  fills_today: number;
  book_router_status: string;
  ws_reconnects: number;
}

// ─── Top-level dashboard snapshot ────────────────────────────────────────────
export interface DashboardSnapshot {
  order_book: OrderBook;
  risk: RiskState;
  pnl: PnLState;
  fills: Fill[];
  zmq: ZmqState;
  circuit_breakers: CircuitBreaker[];
  gateway: GatewayState;
  engine_status: 'running' | 'paused' | 'error';
  mode: 'PAPER' | 'LIVE';
  open_orders: number;
  // No beta model exists in the codebase yet — null, not a fabricated number.
  beta: number | null;
}
