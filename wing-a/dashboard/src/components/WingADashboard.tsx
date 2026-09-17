import React from 'react';
import { useDashboardStore } from '../store/dashboardStore';
import { usePolling } from '../hooks/usePolling';
import { Sidebar } from './Sidebar/Sidebar';
import { TopBar } from './TopBar/TopBar';
import { Panel } from './Panel';
import { OrderBook } from './OrderBook/OrderBook';
import { RiskManager } from './RiskManager/RiskManager';
import { PnLTracker } from './PnLTracker/PnLTracker';
import { FillFeed } from './FillFeed/FillFeed';
import { ZmqSignals } from './ZmqSignals/ZmqSignals';
import { CircuitBreaker } from './CircuitBreaker/CircuitBreaker';
import { AlpacaGateway } from './AlpacaGateway/AlpacaGateway';

// ─── Metric card ─────────────────────────────────────────────────────────────
interface MetricProps {
  label: string;
  value: string;
  sub: string;
  color?: string;
}
const Metric: React.FC<MetricProps> = ({ label, value, sub, color }) => (
  <div style={metricStyles.card}>
    <div style={metricStyles.label}>{label}</div>
    <div style={{ ...metricStyles.val, color: color ?? 'var(--color-text-primary, #111)' }}>{value}</div>
    <div style={metricStyles.sub}>{sub}</div>
  </div>
);
const metricStyles: Record<string, React.CSSProperties> = {
  card: {
    background: 'var(--color-bg-secondary, #f8f8f7)',
    borderRadius: 8,
    padding: '10px 12px',
    fontFamily: '"DM Mono", monospace',
  },
  label: { fontSize: 9, textTransform: 'uppercase', letterSpacing: '0.8px', color: 'var(--color-text-tertiary, #aaa)', marginBottom: 4 },
  val: { fontSize: 18, fontWeight: 500, lineHeight: 1 },
  sub: { fontSize: 9, color: 'var(--color-text-tertiary, #aaa)', marginTop: 3 },
};

// ─── Main dashboard ───────────────────────────────────────────────────────────
export const WingADashboard: React.FC = () => {
  usePolling(2000);
  const data = useDashboardStore((s) => s.data);

  const pnl = data?.pnl.session ?? 0;
  const drawdown = data?.risk.metrics.find((m) => m.label === 'Daily drawdown');
  const zmqLat = data?.zmq.latency_ms ?? 0;

  return (
    <div style={styles.shell}>
      <Sidebar />

      <div style={styles.main}>
        <TopBar />

        <div style={styles.content}>

          {/* ── Metrics row ── */}
          <div style={styles.metricsGrid}>
            <Metric
              label="Session PnL"
              value={`${pnl >= 0 ? '+' : ''}$${Math.abs(pnl).toLocaleString()}`}
              sub="unrealised incl."
              color={pnl >= 0 ? '#22c55e' : '#ef4444'}
            />
            <Metric
              label="Open orders"
              value={String(data?.open_orders ?? '—')}
              sub="via order_manager"
            />
            <Metric
              label="ZMQ latency"
              value={zmqLat > 0 ? `${zmqLat.toFixed(1)}ms` : '—'}
              sub="pub → exec"
              color={zmqLat > 3 ? '#f59e0b' : undefined}
            />
            <Metric
              label="Portfolio β"
              value={data?.beta?.toFixed(2) ?? '—'}
              sub="vs SPY"
            />
            <Metric
              label="Drawdown"
              value={drawdown ? `-${drawdown.current}%` : '—'}
              sub={`max -${drawdown?.limit ?? 3}% gate`}
              color={drawdown?.status === 'warn' ? '#f59e0b' : '#ef4444'}
            />
          </div>

          {/* ── Row 1: Order book + Risk ── */}
          <div style={styles.row2}>
            <Panel
              title="Flat order book"
              file="flat_order_book.hpp"
              badge={{ label: data?.order_book.ticker ?? 'AAPL', variant: 'ok' }}
            >
              <OrderBook data={data?.order_book ?? null} />
            </Panel>
            <Panel
              title="Risk manager"
              file="risk_manager.hpp"
              badge={{ label: data?.risk.overall ?? '…', variant: data?.risk.overall === 'PASS' ? 'ok' : data?.risk.overall === 'WARN' ? 'warn' : 'err' }}
            >
              <RiskManager data={data?.risk ?? null} />
            </Panel>
          </div>

          {/* ── Row 2: PnL + Fills ── */}
          <div style={styles.row2}>
            <Panel title="PnL tracker" file="pnl/tracker.py" badge={{ label: 'Intraday', variant: 'info' }}>
              <PnLTracker data={data?.pnl ?? null} />
            </Panel>
            <Panel
              title="Fill subscriber"
              file="gateway/fill_subscriber.py"
              badge={{ label: `${data?.fills.length ?? 0} fills`, variant: 'ok' }}
            >
              <FillFeed fills={data?.fills ?? []} />
            </Panel>
          </div>

          {/* ── Row 3: ZMQ + CB + Gateway ── */}
          <div style={styles.row3}>
            <Panel
              title="ZMQ signals"
              file="zmq_signal_consumer.hpp"
              badge={{ label: data?.zmq.socket_status === 'connected' ? 'SUB ●' : 'SUB ○', variant: data?.zmq.socket_status === 'connected' ? 'ok' : 'warn' }}
            >
              <ZmqSignals data={data?.zmq ?? null} />
            </Panel>
            <Panel title="Circuit breaker" file="circuit_breaker_explainer.py" badge={{ label: 'NOMINAL', variant: 'ok' }}>
              <CircuitBreaker breakers={data?.circuit_breakers ?? []} />
            </Panel>
            <Panel title="Alpaca gateway" file="gateway/alpaca_feed.py" badge={{ label: data?.gateway.connected ? 'LIVE' : 'DOWN', variant: data?.gateway.connected ? 'ok' : 'err' }}>
              <AlpacaGateway data={data?.gateway ?? null} />
            </Panel>
          </div>

        </div>
      </div>
    </div>
  );
};

const styles: Record<string, React.CSSProperties> = {
  shell: {
    display: 'flex',
    height: '100vh',
    overflow: 'hidden',
    background: 'var(--color-bg-tertiary, #f4f4f2)',
    fontFamily: '"DM Mono", "Fira Code", monospace',
  },
  main: {
    flex: 1,
    display: 'flex',
    flexDirection: 'column',
    overflow: 'hidden',
    minWidth: 0,
  },
  content: {
    flex: 1,
    overflowY: 'auto',
    padding: 16,
    display: 'flex',
    flexDirection: 'column',
    gap: 12,
  },
  metricsGrid: {
    display: 'grid',
    gridTemplateColumns: 'repeat(5, 1fr)',
    gap: 8,
  },
  row2: {
    display: 'grid',
    gridTemplateColumns: '1fr 1fr',
    gap: 12,
  },
  row3: {
    display: 'grid',
    gridTemplateColumns: '1fr 1fr 1fr',
    gap: 12,
  },
};
