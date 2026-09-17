import { create } from 'zustand';
import type { DashboardSnapshot } from '../types';
import { fetchSnapshot } from '../api/client';

interface DashboardStore {
  data: DashboardSnapshot | null;
  loading: boolean;
  error: string | null;
  lastUpdated: Date | null;
  activeTab: string;
  selectedTicker: string;

  setActiveTab: (tab: string) => void;
  setSelectedTicker: (ticker: string) => void;
  refresh: () => Promise<void>;
  startPolling: (intervalMs?: number) => () => void;
}

export const useDashboardStore = create<DashboardStore>((set, get) => ({
  data: null,
  loading: false,
  error: null,
  lastUpdated: null,
  activeTab: 'overview',
  selectedTicker: 'AAPL',

  setActiveTab: (tab) => set({ activeTab: tab }),
  setSelectedTicker: (ticker) => set({ selectedTicker: ticker }),

  refresh: async () => {
    set({ loading: true, error: null });
    try {
      const data = await fetchSnapshot();
      set({ data, loading: false, lastUpdated: new Date() });
    } catch (err) {
      // Fail loudly: keep whatever real data is already on screen (or
      // null, if this is the first fetch), surface the real error, and
      // do NOT paper over it with fabricated numbers. A blank/error
      // dashboard is the correct signal that something upstream is down.
      set({
        loading: false,
        error: `API unreachable: ${err instanceof Error ? err.message : 'unknown error'}`,
      });
    }
  },

  startPolling: (intervalMs = 2000) => {
    const { refresh } = get();
    refresh();
    const id = setInterval(refresh, intervalMs);
    return () => clearInterval(id);
  },
}));
