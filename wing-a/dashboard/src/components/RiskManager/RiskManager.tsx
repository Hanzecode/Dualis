import React from 'react';
import type { RiskState } from '../../types';

interface Props {
  data: RiskState | null;
}

const STATUS_COLOR: Record<string, string> = {
  ok: '#22c55e',
  warn: '#f59e0b',
  breach: '#ef4444',
};

const BADGE_STYLE: Record<string, React.CSSProperties> = {
  PASS: { background: 'rgba(34,197,94,0.1)', color: '#16a34a', border: '0.5px solid rgba(34,197,94,0.3)' },
  WARN: { background: 'rgba(245,158,11,0.1)', color: '#b45309', border: '0.5px solid rgba(245,158,11,0.3)' },
  BREACH: { background: 'rgba(239,68,68,0.1)', color: '#b91c1c', border: '0.5px solid rgba(239,68,68,0.3)' },
};

export const RiskManager: React.FC<Props> = ({ data }) => {
  if (!data) return <div style={styles.empty}>Loading risk metrics…</div>;

  return (
    <div role="region" aria-label="Risk manager">
      <div style={styles.overall}>
        <span style={styles.overallLabel}>Overall status</span>
        <span style={{ ...styles.badge, ...BADGE_STYLE[data.overall] }}>{data.overall}</span>
      </div>

      <div style={styles.rows}>
        {data.metrics.map((m) => (
          <div key={m.label} style={styles.metricRow}>
            <div style={styles.rowHd}>
              <span style={styles.label}>{m.label}</span>
              <span style={{ ...styles.note, color: STATUS_COLOR[m.status] }}>
                {m.current}{m.unit} / {m.limit}{m.unit}
              </span>
            </div>
            <div style={styles.barBg} role="progressbar" aria-valuenow={m.pct} aria-valuemin={0} aria-valuemax={100}>
              <div
                style={{
                  ...styles.barFill,
                  width: `${m.pct}%`,
                  background: STATUS_COLOR[m.status],
                }}
              />
            </div>
          </div>
        ))}
      </div>
    </div>
  );
};

const styles: Record<string, React.CSSProperties> = {
  empty: { fontSize: 11, color: 'var(--color-text-tertiary, #aaa)', padding: 8 },
  overall: {
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'space-between',
    marginBottom: 12,
  },
  overallLabel: { fontSize: 11, color: 'var(--color-text-secondary, #666)', fontFamily: '"DM Mono", monospace' },
  badge: { fontSize: 10, padding: '2px 8px', borderRadius: 20, fontWeight: 500, letterSpacing: 0.5 },
  rows: { display: 'flex', flexDirection: 'column', gap: 10 },
  metricRow: {},
  rowHd: {
    display: 'flex',
    justifyContent: 'space-between',
    fontSize: 11,
    fontFamily: '"DM Mono", monospace',
    marginBottom: 4,
    color: 'var(--color-text-secondary, #555)',
  },
  label: {},
  note: { fontSize: 10, fontFamily: 'monospace' },
  barBg: {
    height: 4,
    background: 'var(--color-bg-secondary, #f0f0f0)',
    borderRadius: 2,
    overflow: 'hidden',
  },
  barFill: {
    height: '100%',
    borderRadius: 2,
    transition: 'width 0.5s ease',
  },
};
