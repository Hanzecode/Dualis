import React from 'react';
import type { Fill } from '../../types';

interface Props {
  fills: Fill[];
}

export const FillFeed: React.FC<Props> = ({ fills }) => {
  if (!fills.length) return <div style={styles.empty}>No fills yet this session.</div>;

  return (
    <div style={styles.list} role="log" aria-label="Fill subscriber feed" aria-live="polite">
      {fills.map((fill) => (
        <div key={fill.id} style={styles.item}>
          <span
            style={{
              ...styles.side,
              ...(fill.side === 'buy' ? styles.sideBuy : styles.sideSell),
            }}
            aria-label={fill.side}
          >
            {fill.side.toUpperCase()}
          </span>

          <div style={styles.middle}>
            <div style={styles.ticker}>
              {fill.ticker}
              <span style={styles.qty}> ×{fill.qty}</span>
            </div>
            <div style={styles.source}>{fill.source}.py → fill_subscriber</div>
          </div>

          <div style={styles.right}>
            <div style={styles.price}>${fill.price.toFixed(2)}</div>
            <div style={styles.time}>{fill.timestamp}</div>
          </div>
        </div>
      ))}
    </div>
  );
};

const styles: Record<string, React.CSSProperties> = {
  empty: { fontSize: 11, color: 'var(--color-text-tertiary, #aaa)', padding: 8 },
  list: { display: 'flex', flexDirection: 'column', gap: 6 },
  item: {
    display: 'grid',
    gridTemplateColumns: 'auto 1fr auto',
    alignItems: 'center',
    gap: 8,
    padding: '6px 8px',
    borderRadius: 6,
    background: 'var(--color-bg-secondary, #f8f8f7)',
    fontFamily: '"DM Mono", monospace',
  },
  side: {
    fontSize: 9,
    fontWeight: 500,
    padding: '2px 6px',
    borderRadius: 20,
    letterSpacing: 0.5,
  },
  sideBuy: {
    background: 'rgba(34,197,94,0.12)',
    color: '#16a34a',
  },
  sideSell: {
    background: 'rgba(239,68,68,0.12)',
    color: '#b91c1c',
  },
  middle: {},
  ticker: {
    fontSize: 12,
    fontWeight: 500,
    color: 'var(--color-text-primary, #111)',
  },
  qty: { fontWeight: 400, color: 'var(--color-text-tertiary, #aaa)' },
  source: { fontSize: 9, color: 'var(--color-text-tertiary, #bbb)', marginTop: 1 },
  right: { textAlign: 'right' },
  price: {
    fontSize: 12,
    fontWeight: 500,
    color: 'var(--color-text-primary, #111)',
  },
  time: { fontSize: 9, color: 'var(--color-text-tertiary, #aaa)', marginTop: 1 },
};
