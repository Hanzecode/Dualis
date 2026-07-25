# Wing A Dashboard — React Frontend

Production-grade React + TypeScript dashboard for the QuantCore Wing A execution engine.

## Stack

| Layer | Tech |
|---|---|
| Framework | React 18 + TypeScript |
| State | Zustand |
| Charts | Chart.js 4 (dynamic import) |
| Fonts | DM Mono |
| Build | Vite 5 |
| API | FastAPI (`dashboard/api.py`) |

## Structure

```
src/
  api/client.ts                  ← fetch wrappers + mock data
  store/dashboardStore.ts        ← Zustand store + polling logic
  hooks/usePolling.ts            ← 2s polling hook
  types/index.ts                 ← all TypeScript interfaces
  components/
    WingADashboard.tsx           ← root layout (metrics + all panels)
    Panel.tsx                    ← reusable card wrapper
    Sidebar/Sidebar.tsx          ← nav linked to activeTab
    TopBar/TopBar.tsx            ← clock, refresh, mode badge, error banner
    OrderBook/OrderBook.tsx      ← flat_order_book.hpp L2 book
    RiskManager/RiskManager.tsx  ← risk_manager.hpp progress bars
    PnLTracker/PnLTracker.tsx    ← tracker.py Chart.js intraday chart
    FillFeed/FillFeed.tsx        ← fill_subscriber.py + alpaca_feed.py
    ZmqSignals/ZmqSignals.tsx    ← zmq_signal_consumer.hpp signal bars
    CircuitBreaker/CircuitBreaker.tsx  ← circuit_breaker_explainer.py
    AlpacaGateway/AlpacaGateway.tsx   ← alpaca_feed.py + book_router.hpp
```

## Quick start

```bash
# 1. Install dependencies
npm install

# 2. Copy env config
cp .env.example .env

# 3. Run with mock data (no API needed)
VITE_MOCK=true npm run dev

# 4. Run against real API (start FastAPI first)
uvicorn dashboard.api:app --reload --port 8000
npm run dev
```

## Wiring to real data

`dashboard_api.py` has stub functions for every data source. Replace each with a real query:

```python
def get_fills(limit: int = 20) -> list[Fill]:
    # Replace with:
    conn = pg_pool.getconn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM fills ORDER BY timestamp DESC LIMIT %s", (limit,))
    rows = cur.fetchall()
    pg_pool.putconn(conn)
    return [Fill(**row) for row in rows]
```

### ZMQ signal consumer
```python
def get_zmq() -> ZmqState:
    # Query your zmq_signal_consumer state —
    # e.g. read from Redis cache written by zmq_signal_consumer.hpp
    signals = redis_client.lrange("signals:latest", 0, 4)
    ...
```

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `VITE_API_URL` | `http://localhost:8000` | FastAPI base URL |
| `VITE_MOCK` | `false` | Use mock data instead of API |
| `VITE_POLL_INTERVAL` | `2000` | Polling cadence (ms) |

## Build for production

```bash
npm run build
# Output: dist/
# Serve via Nginx or `vite preview`
```
