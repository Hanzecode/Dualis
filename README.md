# Dualis: A Dual-Wing Algorithmic Trading Engine

Dualis is a paper-trading system built to mirror how a quantitative fund is structured internally: a **research and portfolio-construction layer** that decides *what* to trade, and a **matching-engine layer** that decides *how* to trade it. Rather than one monolithic script, it is split into two independently deployable services — **Wing B** (analytics and signal generation) and **Wing A** (portfolio construction and execution) — that communicate through a versioned file contract. Either wing can be developed, tested, or restarted without touching the other.

It covers the stack end to end: **ML-based alpha modeling, a human-analyst override path, a multithreaded C++ matching engine with an O(1) array-based price book, ZeroMQ between the Python execution layer and the C++ engine, optional TimescaleDB persistence, and a live FastAPI + React monitoring dashboard.** It runs entirely on your machine — no cloud account is needed, and Docker is only used for the optional TimescaleDB.

## Table of Contents
- [Architecture Overview](#architecture-overview)
- [What Is Real and What Is Simulated](#what-is-real-and-what-is-simulated)
- [Data Flow: Signal to Fill, by Exact File](#data-flow-signal-to-fill-by-exact-file)
- [Wing B: The Analytics & Signal Generation Core](#wing-b-the-analytics--signal-generation-core)
- [The Signal Contract: Bridging Wing B and Wing A](#the-signal-contract-bridging-wing-b-and-wing-a)
- [Wing A: The Portfolio & Execution Core](#wing-a-the-portfolio--execution-core)
- [Optional Infrastructure](#optional-infrastructure)
- [Getting Started](#getting-started)
- [Design Decisions Worth Highlighting](#design-decisions-worth-highlighting)
- [Known Limitations](#known-limitations)

---

## Architecture Overview

There are two service boundaries, and they use different transports on purpose:

- **Wing B → Wing A: a polled local file directory** (`wing-bb/signals_out/signals/`). Wing B writes one JSON file per signal; Wing A polls the directory. This is the research/execution decoupling — neither side needs the other to be running.
- **Inside Wing A: ZeroMQ** between the Python execution layer and the C++ engine — PUSH/PULL for orders (and market-depth updates) going in, PUB/SUB for fills coming back. This is the low-latency hot path.

The two wings:

- **Wing B (Python analytics)** — data ingestion (yfinance prices and fundamentals, FRED macro), a gradient-boosted valuation model, and a Streamlit app that includes a human-analyst signal publisher. Its only output to Wing A is trading signals in a strict JSON contract.
- **Wing A (Python + C++ execution)** — ingests signals, sizes them into orders, runs them through a pre-trade risk gate, and hands them to a C++ matching engine. Portfolio logic stays in Python for iteration speed; matching and the second risk gate are C++.

Wing A can be exercised without a live model by hand-writing signal files into the signal directory.

## What Is Real and What Is Simulated

| Real | Simulated |
|---|---|
| Live bid/ask quotes from Alpaca's market-data WebSocket | Fills, positions, and cash |
| Real price/fundamental/macro data (yfinance, FRED) feeding a real model | The account: a $50,000 in-memory cash ledger (`STARTING_CASH_USD`) |
| The order book, matching, and risk logic | The counterparty: there is none |

Alpaca is used **only as a quote feed** — no order is ever sent to Alpaca, and your Alpaca account balance is never read, so a $0 account works. Orders go to the local C++ engine.

Because no one else trades in the local book, a resting limit order fills only when the **live quote crosses it**: `BookRouter` checks every incoming quote against our resting orders, and if the market has moved through one it injects a synthetic opposite-side market order that fills through the normal matching path, at our limit price. A limit order priced at the mid can therefore take several ticks — or minutes — to fill, and is not guaranteed to fill at all.

## Data Flow: Signal to Fill, by Exact File

The diagram traces one signal — `{"symbol":"AAPL","signal":0.8,"confidence":0.8,"target_qty":400,"limit_price_bps":0}` — through every file it touches, from Wing B to a fill on the dashboard.

```mermaid
graph TD
    subgraph WingB["WING B — Analytics & Signal Generation"]
        ETL["run_etl.py → etl/pipeline.py<br/>yfinance + FRED → SQLite"]
        SCORE["run_score.py → scoring/scorer.py<br/>GradientBoostingRegressor → alpha score<br/>(looped, ~every 30s)"]
        ANALYST["dashboard/pages/5_signal_publisher.py<br/>human analyst UI (Streamlit)"]
        DB[("SQLite: analyst_signals<br/>audit trail")]
        WRITER["common/signal_writer.py<br/>validate + serialize to contract"]

        ETL --> SCORE
        SCORE --> WRITER
        ANALYST --> DB
        ANALYST --> WRITER
    end

    SIGDIR[("wing-bb/signals_out/signals/*.json<br/>the contract")]
    WRITER -- "writes AlphaSignal JSON" --> SIGDIR

    subgraph WingA["WING A — Portfolio Construction & Execution (Python)"]
        INGEST["signal_ingestion/ingestor.py<br/>poll dir every 10s, validate"]
        CONSTRUCT["portfolio/constructor.py<br/>vol-target sizing, delta calc"]
        RISK["risk/gate.py<br/>Python pre-trade gate"]
        OM["order_manager/manager.py<br/>TWAP slicing, ZMQ serialize"]
        ALPACA["gateway/alpaca_feed.py<br/>live quotes, WebSocket"]

        SIGDIR -- "polls" --> INGEST
        ALPACA -. "live mid price" .-> CONSTRUCT
        ALPACA -. "live mid price" .-> RISK
        INGEST -- "AlphaSignal" --> CONSTRUCT
        CONSTRUCT -- "Order" --> RISK
        RISK -- "approved" --> OM
    end

    ZMQPUSH{{"ZeroMQ PUSH → PULL<br/>tcp://*:5557"}}
    OM -- "ORDER" --> ZMQPUSH
    ALPACA -- "MARKET_DEPTH" --> ZMQPUSH

    subgraph CppCore["C++ EXECUTION CORE"]
        RECV["push_pull_receiver.hpp<br/>receiver thread, parse JSON"]
        ENGINE["execution_engine.hpp<br/>route by symbol"]
        RM["risk_manager.hpp<br/>C++ pre-trade check + post-trade position/PnL"]
        ROUTER["book_router.hpp<br/>sync flat + tree books, crossing check"]
        BOOK["order_book.cpp<br/>match_market / match_limit"]
        FLAT["flat_order_book.hpp<br/>O(1) price discovery"]
        ONTRADE["execution_engine.hpp::on_trade<br/>update risk state, publish fill"]

        ZMQPUSH --> RECV --> ENGINE
        ENGINE -- "check()" --> RM
        ENGINE --> ROUTER
        ROUTER --> BOOK
        ROUTER --> FLAT
        BOOK -- "Trade callback" --> ONTRADE
        ONTRADE -- "on_trade()" --> RM
    end

    ZMQPUB{{"ZeroMQ PUB → SUB<br/>tcp://*:5556"}}
    ONTRADE --> ZMQPUB

    subgraph Return["FILL RETURN PATH (Python)"]
        FILLSUB["gateway/fill_subscriber.py"]
        MAIN["main.py<br/>_on_fill_from_engine fan-out"]
        PNL["pnl/tracker.py<br/>positions, PnL, cash ledger"]
        TS[("TimescaleDB<br/>optional")]
        API["dashboard/api.py<br/>FastAPI, read-only GETs"]
        REACT["React dashboard<br/>polls /api/snapshot every 2s"]

        ZMQPUB --> FILLSUB --> MAIN
        MAIN --> OM
        MAIN --> PNL
        MAIN --> API
        PNL -.-> TS
        API --> REACT
    end
```

**Reading the diagram:** a signal is born in Wing B and lands in the signal directory as a JSON file. Wing A polls it, sizes it against a live Alpaca quote into a limit order, passes the Python risk gate, and sends it over ZeroMQ. In the C++ core it passes a second, independent risk check and rests in the order book. Meanwhile every Alpaca quote is also pushed into the same engine as a `MARKET_DEPTH` update; when a quote crosses a resting order, the trade fires, the engine updates its risk state and publishes the fill on a PUB socket, and Wing A fans it out to the order manager, the PnL tracker, and the dashboard.

---

## Wing B: The Analytics & Signal Generation Core

Wing B owns data processing and modeling. It blends **systematic, ML-driven signals** with **discretionary, human-driven signals** through the same publication path, so Wing A never needs to know which kind it is consuming — both arrive as the same `AlphaSignal` JSON.

### 1. Data pipeline

`run_etl.py` → `etl/pipeline.py` pulls prices and fundamentals from **yfinance** and macro series from **FRED** (requires a free `FRED_API_KEY` in `wing-bb/.env`), transforms them with Polars, and loads them into SQLite (`wing_b_local.db`; set `DATABASE_URL` to use Postgres). The universe is 16 stocks across four sectors.

If a live fetch fails, the pipeline **raises** rather than substituting fake data. The old fake-data fallback still exists but is opt-in: set `ALLOW_SYNTHETIC_DATA=true` for a deliberate offline/demo run. (Fundamentals that Yahoo omits for individual fields are filled per-field and flagged in a `data_source` column.)

### 2. ML-driven alpha generation

`scoring/scorer.py` and `analytics/valuation.py` implement the systematic model:

- **Model** — a scikit-learn `GradientBoostingRegressor` (200 trees, `max_depth=2`, `learning_rate=0.05`) trained to predict a "fair" P/E from `rev_growth`, `margin`, `momentum_6m` and `volatility`. The actual P/E is deliberately excluded from the features to avoid target leakage. Shallow trees are chosen because the universe is small and deeper trees would overfit. SHAP values explain individual predictions in the Streamlit valuation page.
- **Signal construction** — the gap between predicted fair P/E and actual P/E ("upside") is cross-sectionally ranked across the universe and mapped into `[-1.0, +1.0]`. Ranking keeps magnitudes comparable across stocks and stops one noisy valuation gap from dominating sizing.
- **Output** — every scored symbol goes through `common/signal_writer.py`, the single choke point all signals — model or human — pass before reaching Wing A.

### 3. Human-in-the-loop: the analyst signal bridge

`dashboard/pages/5_signal_publisher.py` (part of the Streamlit app, `streamlit run wing-bb/dashboard/app.py`) lets an analyst inject a discretionary idea into the same path the model uses: ticker, direction, conviction (mapped to `confidence`), and a written thesis. The thesis is stored in the `analyst_signals` table (SQLite locally) as an audit trail, then written through `signal_writer.py` like any other signal. Wing A picks it up on its next poll.

### 4. Scheduling

`run_etl.py` and `run_score.py` are plain scripts. Locally, run the ETL once (or daily) and loop the scorer:

```bash
while true; do python run_score.py; sleep 30; done
```

30 seconds is a testing cadence chosen for fast feedback. The fundamentals being scored do not change intraday, so re-scoring mostly re-emits near-identical scores — but with fresh timestamps, which keeps signals inside Wing A's 300 s staleness window and gives Wing A a fresh quote to price each new limit order against.

Airflow DAG definitions (`wing-bb/dags/`) exist as an option for scheduled deployment, but Airflow is **not** part of the local run path and nothing here requires it.

---

## The Signal Contract: Bridging Wing B and Wing A

Every producer writes through `common/signal_writer.py`, which enforces one JSON schema. This contract *is* the boundary between the wings:

```json
{
  "symbol": "MSFT",
  "signal": -0.875,
  "confidence": 0.8687,
  "target_qty": 437,
  "limit_price_bps": 0,
  "generated_at": "2026-09-17T18:41:44.275104Z"
}
```

| Field | Range / Type | Role |
|---|---|---|
| `symbol` | ticker string | Which instrument this idea concerns |
| `signal` | `[-1.0, +1.0]` | Direction (sign) and raw conviction |
| `confidence` | `[0.0, 1.0]` | Trust in the call — scales position size, and gates the signal out entirely below a minimum |
| `target_qty` | integer, shares | Wing B's suggested ceiling — Wing A's own vol-targeted size is capped at this |
| `limit_price_bps` | integer, price × 10,000 | `0` means "no opinion": Wing A prices a **limit** order at the live mid. Non-zero pins that limit price |
| `generated_at` | ISO-8601 UTC | Freshness check — Wing A rejects anything older than its staleness window |

---

## Wing A: The Portfolio & Execution Core

### 1. Signal ingestion

`signal_ingestion/ingestor.py` polls the signal directory (recursively) every **10 s**, skipping files it has already processed. Each new file is validated — schema, staleness (max age **300 s**), universe membership, and minimum thresholds (`|signal| ≥ 0.10`, `confidence ≥ 0.50`) — and turned into an `AlphaSignal`.

A file handoff means either wing can restart independently. Because signals expire after 300 s, Wing A resumes from the next fresh batch after a restart instead of replaying stale ideas. If a signal arrives before the first quote for its symbol (it happens in the first second after startup), the constructor drops it with a "No market data yet" warning and the next batch picks the symbol up.

### 2. Portfolio construction

`portfolio/constructor.py` turns a validated signal into an order:

1. **Price check** — needs a live mid price for the symbol; otherwise the signal is dropped.
2. **Vol-targeted size** — `_vol_target_size()` scales size inversely to estimated volatility (target 15% annualised) against a fixed $10,000 per-position risk budget, scaled by `confidence` and capped by `target_qty`.
3. **Delta** — target position minus current position is the quantity actually ordered, so a repeated signal on an already-held position doesn't double up.
4. **Order** — side from the sign of the delta; always a **LIMIT** order, at `limit_price_bps` if the signal supplied one, otherwise at the live mid (avoids crossing the spread). Quantity is capped at the 1,000-share order-size limit.

### 3. Order execution and the C++ core

- **`risk/gate.py`** — the Python pre-trade gate; see [Risk Management](#4-risk-management).
- **`order_manager/manager.py`** — orders over 500 shares are TWAP-sliced into `total // 500` child orders sent 30 s apart; smaller orders go out as one. Each order moves through a 7-state lifecycle (`NEW → SENT → OPEN → PARTIALLY_FILLED → FILLED / CANCELLED / REJECTED`) mirroring the C++ `OrderStatus`, and ZMQ send failures retry with backoff.
- **`gateway/alpaca_feed.py`** — besides feeding prices to the constructor and risk gate, it forwards every quote to the engine as a `MARKET_DEPTH` message.
- **`push_pull_receiver.hpp`** — C++ PULL socket on its own receiver thread; parses `ORDER`, `CANCEL` and `MARKET_DEPTH` messages.
- **`execution_engine.hpp`** — routes to the per-symbol router (a `shared_mutex` guards the router map), runs the C++ pre-trade check, and on each trade updates risk state and publishes the fill.
- **`book_router.hpp`** — owns and syncs the two book representations: a `std::map` tree for order lifecycle and matching, and a flat array (`flat_order_book.hpp`) for O(1) best-bid/ask reads. It also performs the quote-crossing check described [above](#what-is-real-and-what-is-simulated).
- **`order_book.cpp`** — price-time-priority matching (`match_market` / `match_limit`), producing `Trade` objects. Each trade records both `taker_side` (the aggressor — the synthetic market order for crossing fills) and `our_side` (the side of *our* position change, which is what risk, PnL and the dashboard consume).

### 4. Risk Management

There are two independent gates, deliberately. The Python gate is the first, richer line of defence and is cheap to change; the C++ gate is the last check before an order rests in the book and does not assume every caller went through the Python one.

| Check | Python `RiskGate` | C++ `RiskManager` |
|---|---|---|
| Circuit breaker / daily loss | $1,000 | $1,000 |
| Order size | 1,000 shares | 1,000 shares |
| Rate limit | 10 orders/s | 50 orders/s |
| Position per symbol | 10,000 shares | 10,000 shares |
| Total notional | $50,000 | — |

The C++ side also tracks positions and realised PnL from fills (`on_trade`, using `our_side`), and keeps its halt flag and counters in `std::atomic`. See [Known Limitations](#known-limitations) for what is configured but not enforced.

### 5. Fills, PnL, and the live dashboard

- **Fill return path** — the engine publishes each fill as `fills {json}` on a PUB socket. `gateway/fill_subscriber.py` receives it and `main.py` fans it out to the order manager, the PnL tracker, and the dashboard state.
- **`pnl/tracker.py`** — keeps positions (VWAP cost basis), realised and unrealised PnL, and a **synthetic cash ledger**: it starts at `STARTING_CASH_USD` (default $50,000), is debited on our buys and credited on our sells, and `equity = cash + marked positions`. Fills and positions are persisted to TimescaleDB if it is reachable; otherwise it logs the failure and carries on.
- **Dashboard** — `dashboard/api.py` is a **read-only** FastAPI app (GET endpoints only, port 8000) and the React app (Vite, port 3000) polls `/api/snapshot` every 2 s through a Zustand store. The Overview screen shows headline metrics (session PnL, open orders, drawdown), a top-of-book **order book** panel, a **risk** panel rendering each limit as a utilisation bar, a **PnL** panel (session / realised / unrealised, cash and equity, intraday chart), and a scrolling **fill feed**. Every panel is labelled with the backend file that produces its data. If the API is unreachable, the dashboard shows an error banner — it never falls back to mock data — and metrics that aren't implemented (portfolio β, ZMQ latency) show "—" rather than an invented number. It is a monitoring tool: there are no controls for cancelling orders or changing limits.

---

## Optional Infrastructure

Nothing is required to run the system. For fill/position persistence, `wing-a/docker-compose.yml` provides a `timescaledb` service (host port 5434):

```bash
cd wing-a
docker-compose up -d timescaledb
psql postgresql://quant:quant@localhost:5434/quantcore -f db/schema.sql
```

The `rds_mock` and `localstack` services in that file are leftovers and are unused. Persistence happens in Python (`PnLTracker`); the C++ `persist_trade()` / `pg_pool.hpp` path is not wired up.

## Getting Started

There is no mocked/demo mode — every step runs the real component. If something is missing (no Alpaca key, `yfinance` unreachable, engine not built), it is meant to fail loudly rather than substitute fake data.

### 0. One-time setup

One shared virtual environment at the repo root serves both wings:

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r wing-a/requirements.txt -r wing-bb/requirements.txt
```

`wing-bb/requirements.txt` still lists `apache-airflow` (only needed for the optional DAGs); it is a large install you can remove if you don't want it.

Create two `.env` files (both git-ignored):

- `wing-a/.env` — `ALPACA_API_KEY`, `ALPACA_SECRET_KEY` (market-data feed only)
- `wing-bb/.env` — `FRED_API_KEY` (free at fred.stlouisfed.org)

Optional overrides: `STARTING_CASH_USD`, `DATABASE_URL`, `ALLOW_SYNTHETIC_DATA`.

The C++ engine needs libzmq, cppzmq, nlohmann-json and the PostgreSQL client libraries (`brew install zeromq cppzmq nlohmann-json`), plus a C++20 compiler.

### 1. Build the C++ engine
```bash
cmake -S wing-a -B wing-a/build
cmake --build wing-a/build
```

### 2. Generate signals — Wing B
```bash
cd wing-bb
python run_etl.py                                     # once
while true; do python run_score.py; sleep 30; done    # keep this running
```

### 3. Run the C++ engine
```bash
./wing-a/build/quantcore
```

### 4. Run Wing A
```bash
cd wing-a
python main.py
```

You should see Wing A ingest signals from the signal directory, size them, pass the risk gate, and send orders to the engine. Fills appear once a live quote crosses a resting order — that can take a few minutes.

### 5. Dashboard
```bash
cd wing-a/dashboard
npm install && npm run dev      # http://localhost:3000
```

### Optional: Wing B analyst UI
```bash
streamlit run wing-bb/dashboard/app.py
```

## Design Decisions Worth Highlighting

- **Two order-book representations, not one.** `order_book.cpp` (tree-based) handles correctness-critical order lifecycle and matching; `flat_order_book.hpp` (array-based) exists so price-discovery reads are O(1) instead of an O(log n) tree traversal on the hot path. `BookRouter` keeps them in sync — and that sync is where a real bug lived: quote updates only touched the flat book, so resting orders never filled until the crossing check was added.
- **Two file/socket boundaries for two different jobs.** A polled file directory decouples research from execution (restart-tolerant, inspectable, no broker to run); ZeroMQ is reserved for the latency-sensitive Python↔C++ path, with PUSH/PULL where delivery matters (orders) and PUB/SUB where it's a broadcast (fills).
- **Polling over push for signals.** Slower than a push, but Wing A can be offline or restarted without Wing B knowing, and every signal is a file you can open and read.
- **Defence in depth on risk.** Two gates in two languages rather than one, so the last check doesn't rely on every upstream caller behaving.
- **Inverse-volatility sizing instead of `signal × constant`.** The intent is that each idea contributes comparable *risk*, not comparable share count (see the volatility-estimate caveat below).
- **TWAP slicing gated by a size threshold.** Small orders go out as a single order with no added latency; only orders above 500 shares are sliced.
- **Fail loudly instead of faking data.** Fetch failures raise, the dashboard shows an error banner instead of mock numbers, and unimplemented metrics display "—".

## Known Limitations

Listed so they aren't a surprise:

- **Concentration limit is not enforced.** `max_concentration` (40%) is configured on both the Python and C++ side, but neither gate checks it. The C++ `RiskManager` also declares `NOTIONAL_LIMIT`, `CONCENTRATION` and `INVALID_PRICE` reject reasons without implementing them.
- **The Python gate doesn't see real fills.** `main.py` reports position updates to `RiskGate.on_fill()` with zero quantity/PnL, so the Python gate's position, notional and daily-loss state stays at zero; its order-size and rate-limit checks are live. The C++ gate does track real positions and daily loss from fills.
- **Rate limits differ** (10/s Python, 50/s C++) — they're configured independently.
- **Volatility estimate is crude.** It is computed from live quote-tick history and annualised with √252 as if the returns were daily, so it usually lands on the 5% floor. Sizing then differentiates mainly on confidence and price rather than true volatility. Feeding daily realised vol from Wing B's price table would fix it. The $10,000 risk budget is also fixed rather than tied to the cash ledger.
- **Fills depend on the market crossing our price.** There is no external liquidity, and a limit priced at the mid can sit unfilled.
- **Self-trades aren't modelled.** If an incoming order matches one of our own resting orders, the trade is recorded from the taker's side only.
- **The order-book panel is top-of-book only.** Full depth lives in the C++ engine and isn't exposed to the Python process.
- **Persistence lives in Python only** (see Optional Infrastructure), and the C++ logger's fallback writes to stderr rather than a log file.
