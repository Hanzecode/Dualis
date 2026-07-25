import React from 'react';

interface Props {
  title: string;
  file?: string;
  badge?: { label: string; variant: 'ok' | 'warn' | 'info' | 'err' };
  children: React.ReactNode;
  fullHeight?: boolean;
}

const BADGE: Record<string, React.CSSProperties> = {
  ok: { background: 'rgba(34,197,94,0.1)', color: '#16a34a', border: '0.5px solid rgba(34,197,94,0.3)' },
  warn: { background: 'rgba(245,158,11,0.1)', color: '#b45309', border: '0.5px solid rgba(245,158,11,0.3)' },
  info: { background: 'rgba(23,104,200,0.1)', color: '#1768C8', border: '0.5px solid rgba(23,104,200,0.25)' },
  err: { background: 'rgba(239,68,68,0.1)', color: '#b91c1c', border: '0.5px solid rgba(239,68,68,0.3)' },
};

export const Panel: React.FC<Props> = ({ title, file, badge, children, fullHeight }) => (
  <div style={{ ...styles.panel, ...(fullHeight ? { display: 'flex', flexDirection: 'column' } : {}) }}>
    <div style={styles.header}>
      <div style={styles.titleGroup}>
        <span style={styles.title}>{title}</span>
        {file && <span style={styles.file}>{file}</span>}
      </div>
      {badge && (
        <span style={{ ...styles.badge, ...BADGE[badge.variant] }}>{badge.label}</span>
      )}
    </div>
    <div style={{ ...styles.body, ...(fullHeight ? { flex: 1, overflow: 'hidden' } : {}) }}>
      {children}
    </div>
  </div>
);

const styles: Record<string, React.CSSProperties> = {
  panel: {
    background: 'var(--color-bg-primary, #fff)',
    border: '0.5px solid var(--color-border, rgba(0,0,0,0.1))',
    borderRadius: 12,
    overflow: 'hidden',
  },
  header: {
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'space-between',
    padding: '10px 14px',
    borderBottom: '0.5px solid var(--color-border, rgba(0,0,0,0.07))',
  },
  titleGroup: { display: 'flex', alignItems: 'baseline', gap: 8 },
  title: {
    fontSize: 11,
    fontWeight: 500,
    color: 'var(--color-text-primary, #111)',
    fontFamily: '"DM Mono", monospace',
  },
  file: {
    fontSize: 9,
    color: 'var(--color-text-tertiary, #bbb)',
    fontFamily: 'monospace',
  },
  badge: {
    fontSize: 9,
    padding: '2px 7px',
    borderRadius: 20,
    fontWeight: 500,
    letterSpacing: 0.3,
  },
  body: { padding: '12px 14px' },
};
