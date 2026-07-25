import React, { useState, useEffect } from 'react';
import { useDashboardStore } from '../../store/dashboardStore';
import { useLastUpdated } from '../../hooks/usePolling';

const TAB_TITLES: Record<string, string> = {
  overview: 'Overview',
  orderbook: 'Order Book',
  risk: 'Risk Manager',
  zmq: 'ZMQ Signal Consumer',
  pushpull: 'Push / Pull Receiver',
  fills: 'Fill Subscriber',
  pnl: 'PnL Tracker',
  pgpool: 'PG Connection Pool',
};

const TAB_FILES: Record<string, string> = {
  overview: 'execution_engine.hpp',
  orderbook: 'flat_order_book.hpp',
  risk: 'risk_manager.hpp',
  zmq: 'zmq_signal_consumer.hpp',
  pushpull: 'push_pull_receiver.hpp',
  fills: 'fill_subscriber.py',
  pnl: 'tracker.py',
  pgpool: 'pg_pool.hpp',
};

export const TopBar: React.FC = () => {
  const activeTab = useDashboardStore((s) => s.activeTab);
  const data = useDashboardStore((s) => s.data);
  const error = useDashboardStore((s) => s.error);
  const loading = useDashboardStore((s) => s.loading);
  const refresh = useDashboardStore((s) => s.refresh);
  const lastUpdated = useLastUpdated();

  const [clock, setClock] = useState('');
  useEffect(() => {
    const tick = () => setClock(new Date().toLocaleTimeString('en-GB'));
    tick();
    const id = setInterval(tick, 1000);
    return () => clearInterval(id);
  }, []);

  const modeColor = data?.mode === 'LIVE'
    ? { bg: 'rgba(34,197,94,0.1)', color: '#16a34a', border: 'rgba(34,197,94,0.3)' }
    : { bg: 'rgba(245,158,11,0.1)', color: '#b45309', border: 'rgba(245,158,11,0.3)' };

  return (
    <header style={styles.wrapper}>
      <div style={styles.topbar}>
        <div style={styles.left}>
          <span style={styles.title}>{TAB_TITLES[activeTab] ?? 'Wing A'}</span>
          <span style={styles.file}>{TAB_FILES[activeTab]}</span>
        </div>
        <div style={styles.right}>
          <span style={styles.updated}>updated {lastUpdated}</span>
          <span style={styles.clock}>{clock}</span>
          <button
            style={styles.btn}
            onClick={() => refresh()}
            disabled={loading}
            aria-label="Refresh dashboard"
          >
            <span style={{ display: 'inline-block', transform: loading ? 'rotate(180deg)' : 'none', transition: 'transform 0.5s' }}>⟳</span>
            {' '}Refresh
          </button>
          <span style={{ ...styles.badge, background: modeColor.bg, color: modeColor.color, border: `0.5px solid ${modeColor.border}` }}>
            {data?.mode ?? '…'}
          </span>
        </div>
      </div>

      {error && (
        <div style={styles.errorBar} role="alert">
          <span style={{ opacity: 0.7 }}>⚠</span> {error}
        </div>
      )}
    </header>
  );
};

const styles: Record<string, React.CSSProperties> = {
  wrapper: { flexShrink: 0 },
  topbar: {
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'space-between',
    padding: '10px 16px',
    borderBottom: '0.5px solid var(--color-border, rgba(0,0,0,0.1))',
    background: 'var(--color-bg-primary, #fff)',
    fontFamily: '"DM Mono", monospace',
  },
  left: { display: 'flex', alignItems: 'baseline', gap: 8 },
  title: { fontSize: 13, fontWeight: 500, color: 'var(--color-text-primary, #111)' },
  file: { fontSize: 10, color: 'var(--color-text-tertiary, #aaa)', fontFamily: 'monospace' },
  right: { display: 'flex', alignItems: 'center', gap: 10 },
  updated: { fontSize: 10, color: 'var(--color-text-tertiary, #bbb)' },
  clock: { fontSize: 11, color: 'var(--color-text-tertiary, #aaa)', fontFamily: 'monospace', minWidth: 60 },
  btn: {
    fontSize: 11, padding: '4px 10px',
    borderRadius: 6, border: '0.5px solid var(--color-border, rgba(0,0,0,0.15))',
    background: 'var(--color-bg-secondary, #f5f5f5)',
    color: 'var(--color-text-secondary, #555)',
    cursor: 'pointer', fontFamily: 'inherit',
  },
  badge: {
    fontSize: 10, padding: '3px 8px', borderRadius: 20,
    fontWeight: 500, letterSpacing: 0.5,
  },
  errorBar: {
    padding: '6px 16px',
    fontSize: 11,
    background: 'rgba(239,68,68,0.08)',
    color: '#b91c1c',
    borderBottom: '0.5px solid rgba(239,68,68,0.2)',
    fontFamily: 'monospace',
  },
};
