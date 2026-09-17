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
