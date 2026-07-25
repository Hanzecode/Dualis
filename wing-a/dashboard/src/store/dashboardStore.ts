import { create } from 'zustand';
import type { DashboardSnapshot } from '../types';
import { fetchSnapshot, getMockSnapshot } from '../api/client';

const MOCK = import.meta.env.VITE_MOCK === 'true';

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
      const data = MOCK ? getMockSnapshot() : await fetchSnapshot();
      set({ data, loading: false, lastUpdated: new Date() });
    } catch (err) {
      // Graceful fallback to mock if API unreachable
      const data = getMockSnapshot();
      set({
        data,
        loading: false,
        error: `API unreachable — showing mock data (${err instanceof Error ? err.message : 'unknown'})`,
        lastUpdated: new Date(),
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
