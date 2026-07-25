import React from 'react';
import type { OrderBook as OrderBookData } from '../../types';

interface Props {
  data: OrderBookData | null;
}

function Level({
  level,
  side,
  maxSize,
}: {
  level: { price: number; size: number; total: number };
  side: 'bid' | 'ask';
  maxSize: number;
}) {
  const pct = Math.min((level.size / maxSize) * 80, 80);
  const priceColor = side === 'bid' ? '#22c55e' : '#ef4444';

  return (
    <div style={{ ...styles.row, position: 'relative' }}>
      <div
        style={{
          ...styles.depthBar,
          width: `${pct}%`,
          background: priceColor,
          [side === 'bid' ? 'right' : 'left']: 0,
        }}
        aria-hidden="true"
      />
      <span style={styles.size}>{level.size.toLocaleString()}</span>
      <span style={{ ...styles.price, color: priceColor }}>${level.price.toFixed(2)}</span>
      <span style={styles.total}>${(level.total / 1000).toFixed(1)}K</span>
    </div>
  );
}

export const OrderBook: React.FC<Props> = ({ data }) => {
  if (!data) return <div style={styles.empty}>Loading order book…</div>;

  const maxSize = Math.max(
    ...data.asks.map((a) => a.size),
    ...data.bids.map((b) => b.size),
  );

  return (
    <div style={styles.wrapper} role="region" aria-label="Order book">
      {/* Header */}
      <div style={styles.colHeader}>
        <span>Size</span>
        <span style={{ textAlign: 'center' }}>Price</span>
        <span style={{ textAlign: 'right' }}>Total</span>
      </div>

      {/* Asks (reversed so tightest ask at bottom) */}
      <div aria-label="Ask levels">
        {[...data.asks].reverse().map((level) => (
          <Level key={level.price} level={level} side="ask" maxSize={maxSize} />
        ))}
      </div>

      {/* Spread */}
      <div style={styles.spread} aria-label={`Spread ${data.spread.toFixed(2)}, mid ${data.mid.toFixed(2)}`}>
        Spread ${data.spread.toFixed(2)} · Mid ${data.mid.toFixed(2)}
      </div>

      {/* Bids */}
      <div aria-label="Bid levels">
        {data.bids.map((level) => (
          <Level key={level.price} level={level} side="bid" maxSize={maxSize} />
        ))}
      </div>
    </div>
  );
};

const styles: React.CSSProperties | Record<string, React.CSSProperties> = {
  wrapper: { fontFamily: '"DM Mono", monospace', fontSize: 11 },
  empty: { fontSize: 11, color: 'var(--color-text-tertiary, #aaa)', padding: 8 },
  colHeader: {
    display: 'grid',
    gridTemplateColumns: '1fr 1fr 1fr',
    fontSize: 9,
    textTransform: 'uppercase',
    letterSpacing: '0.8px',
    color: 'var(--color-text-tertiary, #aaa)',
    marginBottom: 6,
    padding: '0 2px',
  },
  row: {
    display: 'grid',
    gridTemplateColumns: '1fr 1fr 1fr',
    padding: '3px 2px',
    overflow: 'hidden',
  },
  depthBar: {
    position: 'absolute',
    top: 0,
    bottom: 0,
    opacity: 0.08,
    borderRadius: 2,
    pointerEvents: 'none',
  },
  size: { color: 'var(--color-text-secondary, #666)', zIndex: 1 },
  price: { textAlign: 'center', zIndex: 1, fontWeight: 500 },
  total: { textAlign: 'right', color: 'var(--color-text-tertiary, #aaa)', zIndex: 1 },
  spread: {
    textAlign: 'center',
    fontSize: 10,
    color: 'var(--color-text-tertiary, #aaa)',
    padding: '5px 0',
    margin: '4px 0',
    borderTop: '0.5px dashed var(--color-border, rgba(0,0,0,0.1))',
    borderBottom: '0.5px dashed var(--color-border, rgba(0,0,0,0.1))',
  },
} as Record<string, React.CSSProperties>;
