import React from 'react';
import type { ZmqState } from '../../types';

interface Props {
  data: ZmqState | null;
}

const SOCKET_COLOR: Record<string, string> = {
  connected: '#22c55e',
  reconnecting: '#f59e0b',
  disconnected: '#ef4444',
};

export const ZmqSignals: React.FC<Props> = ({ data }) => {
  if (!data) return <div style={styles.empty}>Waiting for ZMQ signals…</div>;

  return (
    <div role="region" aria-label="ZMQ signal consumer">
      {/* Meta row */}
      <div style={styles.meta}>
        <div style={styles.metaItem}>
          <span
            style={{ ...styles.dot, background: SOCKET_COLOR[data.socket_status] }}
            aria-hidden="true"
          />
          <span style={styles.metaLabel}>{data.socket_status}</span>
        </div>
        <div style={styles.metaItem}>
          <span style={styles.metaLabel}>{data.latency_ms.toFixed(1)}ms latency</span>
        </div>
        <div style={styles.metaItem}>
          <span style={styles.metaLabel}>{data.messages_recv.toLocaleString()} recv</span>
        </div>
      </div>

      {/* Signal bars */}
      <div style={styles.list} aria-label="Alpha signals from Wing B">
        {data.signals.map((sig) => {
          const pct = Math.abs(sig.value) * 100;
          const color = sig.direction > 0 ? '#22c55e' : '#ef4444';
          const sign = sig.direction > 0 ? '+' : '';
          return (
            <div key={sig.ticker} style={styles.sigRow}>
              <span style={styles.ticker}>{sig.ticker}</span>
              <div style={styles.barTrack} role="meter" aria-valuenow={pct} aria-valuemin={0} aria-valuemax={100}>
                <div style={{ ...styles.barFill, width: `${pct}%`, background: color }} />
              </div>
              <span style={{ ...styles.sigVal, color }}>{sign}{sig.value.toFixed(2)}</span>
              <span style={styles.strategy}>{sig.strategy}</span>
            </div>
          );
        })}
      </div>
    </div>
  );
};

const styles: Record<string, React.CSSProperties> = {
  empty: { fontSize: 11, color: 'var(--color-text-tertiary, #aaa)', padding: 8 },
  meta: {
    display: 'flex',
    gap: 10,
    marginBottom: 10,
    padding: '6px 8px',
    background: 'var(--color-bg-secondary, #f8f8f7)',
    borderRadius: 6,
    flexWrap: 'wrap',
  },
  metaItem: { display: 'flex', alignItems: 'center', gap: 5 },
  dot: { width: 6, height: 6, borderRadius: '50%', display: 'inline-block', animation: 'pulse 2s infinite' },
  metaLabel: { fontSize: 10, color: 'var(--color-text-secondary, #666)', fontFamily: '"DM Mono", monospace' },
  list: { display: 'flex', flexDirection: 'column', gap: 6 },
  sigRow: {
    display: 'grid',
    gridTemplateColumns: '44px 1fr 40px 60px',
    alignItems: 'center',
    gap: 8,
    padding: '5px 6px',
    borderRadius: 5,
    border: '0.5px solid var(--color-border, rgba(0,0,0,0.08))',
    fontFamily: '"DM Mono", monospace',
  },
  ticker: { fontSize: 11, fontWeight: 500, color: 'var(--color-text-primary, #111)' },
  barTrack: {
    height: 3,
    background: 'var(--color-bg-secondary, #f0f0f0)',
    borderRadius: 2,
    overflow: 'hidden',
  },
  barFill: { height: '100%', borderRadius: 2, transition: 'width 0.4s ease' },
  sigVal: { fontSize: 10, textAlign: 'right', fontWeight: 500 },
  strategy: { fontSize: 9, color: 'var(--color-text-tertiary, #bbb)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' },
};
