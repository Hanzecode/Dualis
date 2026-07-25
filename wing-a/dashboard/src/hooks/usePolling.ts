import { useEffect, useRef } from 'react';
import { useDashboardStore } from '../store/dashboardStore';

/**
 * Starts polling the dashboard API and cleans up on unmount.
 * @param intervalMs  Polling cadence in milliseconds (default 2000)
 */
export function usePolling(intervalMs = 2000) {
  const startPolling = useDashboardStore((s) => s.startPolling);
  const stopRef = useRef<(() => void) | null>(null);

  useEffect(() => {
    stopRef.current = startPolling(intervalMs);
    return () => stopRef.current?.();
  }, [intervalMs, startPolling]);
}

/**
 * Returns the time since lastUpdated as a human-readable string.
 */
export function useLastUpdated(): string {
  const lastUpdated = useDashboardStore((s) => s.lastUpdated);
  if (!lastUpdated) return 'never';
  const diff = Math.floor((Date.now() - lastUpdated.getTime()) / 1000);
  if (diff < 5) return 'just now';
  if (diff < 60) return `${diff}s ago`;
  return `${Math.floor(diff / 60)}m ago`;
}
