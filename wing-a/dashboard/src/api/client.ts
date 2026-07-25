import type {
  DashboardSnapshot,
  OrderBook,
  RiskState,
  PnLState,
  Fill,
  ZmqState,
  CircuitBreaker,
  GatewayState,
} from '../types';

const BASE = import.meta.env.VITE_API_URL ?? 'http://localhost:8000';

async function get<T>(path: string): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    headers: { 'Content-Type': 'application/json' },
  });
  if (!res.ok) throw new Error(`API ${path} → ${res.status}`);
  return res.json() as Promise<T>;
}

// ─── dashboard/api.py routes ──────────────────────────────────────────────────

/** GET /api/snapshot — full dashboard snapshot (primary polling endpoint) */
export const fetchSnapshot = () => get<DashboardSnapshot>('/api/snapshot');

/** GET /api/orderbook?ticker=AAPL */
export const fetchOrderBook = (ticker = 'AAPL') =>
  get<OrderBook>(`/api/orderbook?ticker=${ticker}`);

/** GET /api/risk */
export const fetchRisk = () => get<RiskState>('/api/risk');

/** GET /api/pnl */
export const fetchPnL = () => get<PnLState>('/api/pnl');

/** GET /api/fills?limit=20 */
export const fetchFills = (limit = 20) =>
  get<Fill[]>(`/api/fills?limit=${limit}`);

/** GET /api/zmq — ZMQ signal consumer state */
export const fetchZmq = () => get<ZmqState>('/api/zmq');

/** GET /api/circuit_breakers */
export const fetchCircuitBreakers = () =>
  get<CircuitBreaker[]>('/api/circuit_breakers');

/** GET /api/gateway — Alpaca gateway status */
export const fetchGateway = () => get<GatewayState>('/api/gateway');

// ─── Mock fallback (used when VITE_MOCK=true or API unreachable) ──────────────
export function getMockSnapshot(): DashboardSnapshot {
  const now = new Date();
  const fmt = (d: Date) => d.toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit' });

  const history = Array.from({ length: 22 }, (_, i) => {
    const t = new Date(now.getTime() - (21 - i) * 15 * 60000);
    const seed = Math.sin(i * 0.7) * 800 + i * 110 + Math.random() * 120;
    return { time: fmt(t), value: Math.round(seed) };
  });

  return {
    engine_status: 'running',
    mode: 'PAPER',
    open_orders: 14,
    beta: 0.73,
    order_book: {
      ticker: 'AAPL',
      spread: 0.08,
      mid: 187.62,
      timestamp: now.toISOString(),
      asks: [
        { price: 187.70, size: 200, total: 37540 },
        { price: 187.66, size: 350, total: 65681 },
        { price: 187.63, size: 500, total: 93815 },
      ],
      bids: [
        { price: 187.55, size: 450, total: 84397 },
        { price: 187.51, size: 300, total: 56253 },
        { price: 187.47, size: 150, total: 28120 },
      ],
    },
    risk: {
      overall: 'PASS',
      metrics: [
        { label: 'Position limit', current: 62, limit: 100, unit: 'K', pct: 62, status: 'ok' },
        { label: 'Daily drawdown', current: 1.2, limit: 3.0, unit: '%', pct: 40, status: 'warn' },
        { label: 'Sector conc.', current: 55, limit: 80, unit: '%', pct: 69, status: 'ok' },
        { label: 'Beta exposure', current: 0.73, limit: 1.5, unit: 'β', pct: 49, status: 'ok' },
        { label: 'Orders / min', current: 30, limit: 100, unit: '', pct: 30, status: 'ok' },
      ],
    },
    pnl: {
      session: 2481,
      unrealised: 840,
      realised: 1641,
      history,
    },
    fills: [
      { id: '1', side: 'buy', ticker: 'AAPL', qty: 100, price: 187.48, timestamp: '14:32:01', source: 'alpaca_feed' },
      { id: '2', side: 'sell', ticker: 'TSLA', qty: 50, price: 248.10, timestamp: '14:29:44', source: 'alpaca_feed' },
      { id: '3', side: 'buy', ticker: 'NVDA', qty: 20, price: 911.20, timestamp: '14:27:18', source: 'alpaca_feed' },
      { id: '4', side: 'sell', ticker: 'MSFT', qty: 75, price: 421.60, timestamp: '14:24:55', source: 'alpaca_feed' },
      { id: '5', side: 'buy', ticker: 'AMZN', qty: 30, price: 185.40, timestamp: '14:21:03', source: 'alpaca_feed' },
    ],
    zmq: {
      latency_ms: 1.8,
      socket_status: 'connected',
      messages_recv: 4821,
      signals: [
        { ticker: 'AAPL', value: 0.78, direction: 1, confidence: 0.91, strategy: 'momentum', received_at: now.toISOString() },
        { ticker: 'TSLA', value: -0.45, direction: -1, confidence: 0.77, strategy: 'mean_rev', received_at: now.toISOString() },
        { ticker: 'NVDA', value: 0.92, direction: 1, confidence: 0.95, strategy: 'momentum', received_at: now.toISOString() },
        { ticker: 'MSFT', value: 0.31, direction: 1, confidence: 0.68, strategy: 'ridge', received_at: now.toISOString() },
        { ticker: 'AMZN', value: -0.12, direction: -1, confidence: 0.55, strategy: 'ridge', received_at: now.toISOString() },
      ],
    },
    circuit_breakers: [
      { name: 'Drawdown gate', state: 'open', reason: '-1.2% / -3%' },
      { name: 'Vol spike guard', state: 'open', reason: 'VIX 16.2' },
      { name: 'Order flood', state: 'open', reason: '30/min < 100' },
      { name: 'Conn watchdog', state: 'half', reason: 'ZMQ retry ×1' },
    ],
    gateway: {
      connected: true,
      stream_active: true,
      symbols_subscribed: 12,
      last_tick_ms: 300,
      fills_today: 24,
      book_router_status: 'AAPL route OK',
      ws_reconnects: 1,
    },
  };
}
