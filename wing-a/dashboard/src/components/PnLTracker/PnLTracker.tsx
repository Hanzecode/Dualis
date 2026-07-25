import React, { useEffect, useRef } from 'react';
import type { PnLState } from '../../types';

interface Props {
  data: PnLState | null;
}

function fmt(v: number) {
  const sign = v >= 0 ? '+' : '';
  return `${sign}$${Math.abs(v).toLocaleString()}`;
}

export const PnLTracker: React.FC<Props> = ({ data }) => {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const chartRef = useRef<any>(null);

  useEffect(() => {
    if (!data || !canvasRef.current) return;

    // Dynamic import of Chart.js to avoid SSR issues
    import('chart.js/auto').then((mod) => {
      const Chart = mod.default;

      if (chartRef.current) {
        chartRef.current.data.labels = data.history.map((p) => p.time);
        chartRef.current.data.datasets[0].data = data.history.map((p) => p.value);
        chartRef.current.update('none');
        return;
      }

      const isDark = window.matchMedia('(prefers-color-scheme: dark)').matches;

      chartRef.current = new Chart(canvasRef.current!, {
        type: 'line',
        data: {
          labels: data.history.map((p) => p.time),
          datasets: [
            {
              data: data.history.map((p) => p.value),
              borderColor: data.session >= 0 ? '#22c55e' : '#ef4444',
              borderWidth: 1.5,
              pointRadius: 0,
              tension: 0.4,
              fill: true,
              backgroundColor: data.session >= 0
                ? 'rgba(34,197,94,0.07)'
                : 'rgba(239,68,68,0.07)',
            },
          ],
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          animation: false,
          plugins: {
            legend: { display: false },
            tooltip: {
              callbacks: { label: (ctx) => `PnL: $${ctx.parsed.y.toLocaleString()}` },
              displayColors: false,
              bodyFont: { family: 'monospace', size: 11 },
            },
          },
          scales: {
            x: {
              ticks: {
                font: { size: 9 },
                color: isDark ? '#666' : '#bbb',
                maxTicksLimit: 6,
                autoSkip: true,
              },
              grid: { display: false },
              border: { display: false },
            },
            y: {
              ticks: {
                font: { size: 9 },
                color: isDark ? '#666' : '#bbb',
                callback: (v) => `$${Number(v).toLocaleString()}`,
              },
              grid: { color: isDark ? 'rgba(255,255,255,0.04)' : 'rgba(0,0,0,0.04)' },
              border: { display: false },
            },
          },
        },
      });
    });

    return () => {
      chartRef.current?.destroy();
      chartRef.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [!!data]);

  // Update chart data without re-creating
  useEffect(() => {
    if (!chartRef.current || !data) return;
    chartRef.current.data.labels = data.history.map((p) => p.time);
    chartRef.current.data.datasets[0].data = data.history.map((p) => p.value);
    chartRef.current.update('none');
  }, [data]);

  if (!data) return <div style={styles.empty}>Loading PnL…</div>;

  const sessionColor = data.session >= 0 ? '#22c55e' : '#ef4444';

  return (
    <div role="region" aria-label="PnL tracker">
      {/* Mini stat row */}
      <div style={styles.statRow}>
        <div style={styles.stat}>
          <div style={styles.statLabel}>Session</div>
          <div style={{ ...styles.statVal, color: sessionColor }}>{fmt(data.session)}</div>
        </div>
        <div style={styles.stat}>
          <div style={styles.statLabel}>Realised</div>
          <div style={styles.statVal}>{fmt(data.realised)}</div>
        </div>
        <div style={styles.stat}>
          <div style={styles.statLabel}>Unrealised</div>
          <div style={styles.statVal}>{fmt(data.unrealised)}</div>
        </div>
      </div>

      {/* Chart */}
      <div style={styles.chartWrap}>
        <canvas
          ref={canvasRef}
          role="img"
          aria-label={`Intraday PnL chart. Session: ${fmt(data.session)}`}
        >
          Intraday PnL: {fmt(data.session)}
        </canvas>
      </div>
    </div>
  );
};

const styles: Record<string, React.CSSProperties> = {
  empty: { fontSize: 11, color: 'var(--color-text-tertiary, #aaa)', padding: 8 },
  statRow: {
    display: 'grid',
    gridTemplateColumns: 'repeat(3, 1fr)',
    gap: 8,
    marginBottom: 12,
  },
  stat: {
    padding: '8px 10px',
    background: 'var(--color-bg-secondary, #f8f8f7)',
    borderRadius: 6,
  },
  statLabel: {
    fontSize: 9,
    textTransform: 'uppercase',
    letterSpacing: '0.8px',
    color: 'var(--color-text-tertiary, #aaa)',
    fontFamily: '"DM Mono", monospace',
    marginBottom: 3,
  },
  statVal: {
    fontSize: 14,
    fontWeight: 500,
    fontFamily: '"DM Mono", monospace',
    color: 'var(--color-text-primary, #111)',
  },
  chartWrap: {
    position: 'relative',
    height: 120,
  },
};
