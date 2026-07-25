import React from 'react';
import { useDashboardStore } from '../../store/dashboardStore';

interface NavItem {
  id: string;
  label: string;
  icon: string;
  file: string;
}

const NAV_SECTIONS: { title: string; items: NavItem[] }[] = [
  {
    title: 'Engine',
    items: [
      { id: 'overview', label: 'Overview', icon: '⊞', file: 'execution_engine.hpp' },
      { id: 'orderbook', label: 'Order book', icon: '≡', file: 'flat_order_book.hpp' },
      { id: 'risk', label: 'Risk gate', icon: '⛨', file: 'risk_manager.hpp' },
    ],
  },
  {
    title: 'Signals',
    items: [
      { id: 'zmq', label: 'ZMQ consumer', icon: '⟳', file: 'zmq_signal_consumer.hpp' },
      { id: 'pushpull', label: 'Push / pull', icon: '↕', file: 'push_pull_receiver.hpp' },
    ],
  },
  {
    title: 'Trading',
    items: [
      { id: 'fills', label: 'Fill subscriber', icon: '✓', file: 'fill_subscriber.py' },
      { id: 'pnl', label: 'PnL tracker', icon: '↗', file: 'tracker.py' },
      { id: 'pgpool', label: 'PG pool', icon: '⬡', file: 'pg_pool.hpp' },
    ],
  },
];

const ZMQ_CHIPS = ['PUB/SUB', 'PUSH/PULL', 'D/R'];

export const Sidebar: React.FC = () => {
  const activeTab = useDashboardStore((s) => s.activeTab);
  const setActiveTab = useDashboardStore((s) => s.setActiveTab);
  const data = useDashboardStore((s) => s.data);

  return (
    <aside style={styles.sidebar}>
      {/* Logo */}
      <div style={styles.logo}>
        <div style={styles.logoName}>QuantCore</div>
        <div style={styles.logoSub}>Wing A · Execution Engine</div>
      </div>

      {/* Nav */}
      <nav style={styles.nav} aria-label="Wing A navigation">
        {NAV_SECTIONS.map((section) => (
          <div key={section.title}>
            <div style={styles.sectionLabel}>{section.title}</div>
            {section.items.map((item) => (
              <button
                key={item.id}
                style={{
                  ...styles.navItem,
                  ...(activeTab === item.id ? styles.navItemActive : {}),
                }}
                onClick={() => setActiveTab(item.id)}
                aria-current={activeTab === item.id ? 'page' : undefined}
              >
                <span style={styles.navIcon} aria-hidden="true">{item.icon}</span>
                <span style={styles.navLabel}>{item.label}</span>
                <span style={styles.navFile}>{item.file}</span>
              </button>
            ))}
          </div>
        ))}
      </nav>

      {/* Status bar */}
      <div style={styles.statusBar}>
        <div style={styles.statusRow}>
          <span
            style={{
              ...styles.statusDot,
              background: data?.engine_status === 'running' ? '#22c55e'
                : data?.engine_status === 'paused' ? '#f59e0b' : '#ef4444',
            }}
            aria-label={`Engine ${data?.engine_status ?? 'unknown'}`}
          />
          <span style={styles.statusLabel}>
            {data?.engine_status === 'running' ? 'Engine running' : data?.engine_status ?? 'Connecting…'}
          </span>
        </div>
        <div style={styles.zmqRow}>
          {ZMQ_CHIPS.map((chip) => (
            <span key={chip} style={styles.zmqChip}>{chip}</span>
          ))}
        </div>
      </div>
    </aside>
  );
};

const styles: Record<string, React.CSSProperties> = {
  sidebar: {
    width: 220,
    flexShrink: 0,
    background: 'var(--color-bg-secondary, #f8f8f7)',
    borderRight: '0.5px solid var(--color-border, rgba(0,0,0,0.1))',
    display: 'flex',
    flexDirection: 'column',
    fontFamily: '"DM Mono", "Fira Code", monospace',
  },
  logo: {
    padding: '16px 16px 14px',
    borderBottom: '0.5px solid var(--color-border, rgba(0,0,0,0.1))',
  },
  logoName: {
    fontSize: 15,
    fontWeight: 500,
    color: 'var(--color-text-primary, #111)',
    letterSpacing: '-0.3px',
  },
  logoSub: {
    fontSize: 9,
    color: 'var(--color-text-tertiary, #999)',
    textTransform: 'uppercase' as const,
    letterSpacing: 1,
    marginTop: 2,
  },
  nav: { padding: 8, flex: 1, overflowY: 'auto' as const },
  sectionLabel: {
    fontSize: 9,
    textTransform: 'uppercase' as const,
    letterSpacing: 1,
    color: 'var(--color-text-tertiary, #aaa)',
    padding: '12px 10px 4px',
  },
  navItem: {
    display: 'flex',
    alignItems: 'center',
    gap: 7,
    width: '100%',
    padding: '6px 10px',
    borderRadius: 6,
    border: 'none',
    background: 'transparent',
    cursor: 'pointer',
    fontSize: 12,
    color: 'var(--color-text-secondary, #555)',
    textAlign: 'left' as const,
    marginBottom: 1,
    transition: 'background 0.1s',
  },
  navItemActive: {
    background: 'var(--color-bg-primary, #fff)',
    color: 'var(--color-text-primary, #111)',
    border: '0.5px solid var(--color-border, rgba(0,0,0,0.1))',
  },
  navIcon: { fontSize: 13, width: 16, textAlign: 'center' as const },
  navLabel: { flex: 1 },
  navFile: {
    fontSize: 8,
    color: 'var(--color-text-tertiary, #bbb)',
    fontFamily: 'monospace',
    overflow: 'hidden',
    textOverflow: 'ellipsis',
    whiteSpace: 'nowrap' as const,
    maxWidth: 80,
  },
  statusBar: {
    padding: 12,
    borderTop: '0.5px solid var(--color-border, rgba(0,0,0,0.1))',
  },
  statusRow: { display: 'flex', alignItems: 'center', gap: 6 },
  statusDot: {
    width: 6, height: 6, borderRadius: '50%',
    animation: 'pulse 2s infinite',
    flexShrink: 0,
  },
  statusLabel: { fontSize: 11, color: 'var(--color-text-secondary, #666)' },
  zmqRow: { display: 'flex', gap: 4, marginTop: 6, flexWrap: 'wrap' as const },
  zmqChip: {
    fontSize: 9, padding: '2px 6px', borderRadius: 20,
    background: 'rgba(23,104,200,0.1)',
    color: 'var(--color-text-info, #1768C8)',
    border: '0.5px solid rgba(23,104,200,0.25)',
  },
};
