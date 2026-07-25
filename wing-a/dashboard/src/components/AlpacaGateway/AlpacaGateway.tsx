import React from 'react';
import type { GatewayState } from '../../types';

interface Props {
  data: GatewayState | null;
}

export const AlpacaGateway: React.FC<Props> = ({ data }) => {
  if (!data) return <div style={styles.empty}>Loading gateway state…</div>;

  const rows: { key: string; value: string; highlight?: boolean }[] = [
    { key: 'Connection', value: data.connected ? 'LIVE' : 'DISCONNECTED', highlight: data.connected },
    { key: 'WS stream', value: data.stream_active ? 'active' : 'inactive' },
    { key: 'Symbols sub.', value: `${data.symbols_subscribed} tickers` },
    { key: 'Last tick', value: `${(data.last_tick_ms / 1000).toFixed(1)}s ago` },
    { key: 'Fills today', value: `${data.fills_today}` },
    { key: 'book_router', value: data.book_router_status },
    { key: 'WS reconnects', value: `${data.ws_reconnects}` },
  ];

  return (
    <div style={styles.rows} role="region" aria-label="Alpaca gateway status">
      {rows.map((row) => (
        <div key={row.key} style={styles.row}>
          <span style={styles.key}>{row.key}</span>
          <span
            style={{
              ...styles.val,
              ...(row.highlight ? styles.valLive : {}),
            }}
          >
            {row.value}
          </span>
        </div>
      ))}
    </div>
  );
};

const styles: Record<string, React.CSSProperties> = {
  empty: { fontSize: 11, color: 'var(--color-text-tertiary, #aaa)', padding: 8 },
  rows: { display: 'flex', flexDirection: 'column', gap: 0, fontFamily: '"DM Mono", monospace' },
  row: {
    display: 'flex',
    justifyContent: 'space-between',
    alignItems: 'center',
    fontSize: 11,
    padding: '5px 0',
    borderBottom: '0.5px solid var(--color-border, rgba(0,0,0,0.07))',
  },
  key: { color: 'var(--color-text-secondary, #666)' },
  val: { fontWeight: 500, color: 'var(--color-text-primary, #111)' },
  valLive: { color: '#16a34a' },
};
