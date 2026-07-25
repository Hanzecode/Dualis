import React from 'react';
import type { CircuitBreaker as CB, BreakerState } from '../../types';

interface Props {
  breakers: CB[];
}

const STATE_STYLE: Record<BreakerState, { label: string; color: string; bg: string; border: string }> = {
  open: { label: 'OPEN', color: '#16a34a', bg: 'rgba(34,197,94,0.08)', border: 'rgba(34,197,94,0.2)' },
  half: { label: 'HALF', color: '#b45309', bg: 'rgba(245,158,11,0.08)', border: 'rgba(245,158,11,0.2)' },
  tripped: { label: 'TRIPPED', color: '#b91c1c', bg: 'rgba(239,68,68,0.08)', border: 'rgba(239,68,68,0.2)' },
};

export const CircuitBreaker: React.FC<Props> = ({ breakers }) => {
  if (!breakers.length) return <div style={styles.empty}>No circuit breaker data.</div>;

  return (
    <div style={styles.grid} role="region" aria-label="Circuit breakers">
      {breakers.map((cb) => {
        const s = STATE_STYLE[cb.state];
        return (
          <div
            key={cb.name}
            style={{ ...styles.card, background: s.bg, border: `0.5px solid ${s.border}` }}
            aria-label={`${cb.name}: ${s.label}`}
          >
            <div style={styles.name}>{cb.name}</div>
            <div style={{ ...styles.stateLabel, color: s.color }}>{s.label}</div>
            <div style={styles.reason}>{cb.reason}</div>
            {cb.triggered_at && (
              <div style={styles.time}>tripped {cb.triggered_at}</div>
            )}
          </div>
        );
      })}
    </div>
  );
};

const styles: Record<string, React.CSSProperties> = {
  empty: { fontSize: 11, color: 'var(--color-text-tertiary, #aaa)', padding: 8 },
  grid: {
    display: 'grid',
    gridTemplateColumns: '1fr 1fr',
    gap: 8,
  },
  card: {
    padding: '8px 10px',
    borderRadius: 8,
    fontFamily: '"DM Mono", monospace',
  },
  name: {
    fontSize: 9,
    textTransform: 'uppercase',
    letterSpacing: '0.8px',
    color: 'var(--color-text-tertiary, #aaa)',
    marginBottom: 3,
  },
  stateLabel: {
    fontSize: 13,
    fontWeight: 500,
    letterSpacing: 0.3,
    marginBottom: 2,
  },
  reason: {
    fontSize: 9,
    color: 'var(--color-text-tertiary, #aaa)',
  },
  time: {
    fontSize: 9,
    color: 'var(--color-text-tertiary, #aaa)',
    marginTop: 2,
  },
};
