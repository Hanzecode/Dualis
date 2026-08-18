# QuantCore: A Dual-Wing Algorithmic Trading Engine

QuantCore is a full-stack, low-latency algorithmic trading system built to mirror how a real quantitative fund is structured internally: a **research and portfolio-construction layer** that decides *what* to trade, and a **matching-engine layer** that decides *how* to trade it. Rather than building one monolithic script, the system is split into two independently deployable services — **Wing B** (analytics and signal generation) and **Wing A** (portfolio construction and execution) — that communicate asynchronously through a versioned file contract. This mirrors the "quant research vs. execution desk" separation used at real trading firms, and it means either wing can be developed, tested, or restarted without touching the other.

The project demonstrates end-to-end ownership across the stack a hedge fund actually runs on: **ML-based alpha modeling, a human-analyst override path, a C++ matching engine with lock-free data structures, ZeroMQ for the hot path, Postgres/TimescaleDB for persistence, and a live React dashboard** — all runnable locally with zero cloud dependencies via Docker.

## Table of Contents
- [Architecture Overview](#architecture-overview)
- [Data Flow: Signal to Fill, by Exact File](#data-flow-signal-to-fill-by-exact-file)
- [Wing B: The Analytics & Signal Generation Core](#wing-b-the-analytics--signal-generation-core)
  - [1. ML-Driven Alpha Generation](#1-ml-driven-alpha-generation)
  - [2. Human-in-the-Loop: The Analyst Signal Bridge](#2-human-in-the-loop-the-analyst-signal-bridge)
  - [3. Orchestration with Airflow](#3-orchestration-with-airflow)
- [The Signal Contract: Bridging Wing B and Wing A](#the-signal-contract-bridging-wing-b-and-wing-a)
- [Wing A: The Portfolio & Execution Core](#wing-a-the-portfolio--execution-core)
  - [1. Signal Ingestion](#1-signal-ingestion)
  - [2. Portfolio Construction](#2-portfolio-construction)
  - [3. Order Execution and the C++ Core](#3-order-execution-and-the-c-core)
  - [4. Risk Management](#4-risk-management)
  - [5. Fills, PnL, and the Live Dashboard](#5-fills-pnl-and-the-live-dashboard)
- [Local Development with Docker](#local-development-with-docker)
- [Getting Started](#getting-started)
- [Design Decisions Worth Highlighting](#design-decisions-worth-highlighting)

---

## Architecture Overview

The system is split into two primary services, decoupled via **S3 (signal files)** and **ZeroMQ (order/fill messages)** so that a slow or crashed component in one wing never blocks the other:

- **Wing B (Python Analytics)** — a suite of scheduled and on-demand services for data ingestion, factor modeling, ML scoring, and human-analyst signal publishing. Its sole output is trading signals, written to a shared store in a strict, versioned JSON contract.
- **Wing A (Python/C++ Execution)** — a hybrid service that ingests those signals, sizes them into concrete orders using volatility targeting, runs them through a pre-trade risk gate, and hands them to a C++ matching engine for low-latency execution against a live order book. The high-level portfolio logic stays in Python for flexibility and fast iteration; the latency-critical matching logic is C++ for raw speed.

This separation is deliberate: Wing B can be re-run, backtested, or entirely re-architected without ever touching the execution path, and Wing A can be stress-tested against synthetic signals without needing a live model.

## Data Flow: Signal to Fill, by Exact File

The diagram below traces one real signal — `{"symbol":"AAPL","signal":0.8,"confidence":0.8,"target_qty":400,"limit_price_bps":0}` — through every file it touches, from generation in Wing B to a filled order appearing on the dashboard.

```mermaid
graph TD
    subgraph WingB["WING B — Analytics & Signal Generation"]
        ETL["dags/wing_b_etl_dag.py<br/>market data ETL, pre-market"]
        SCOREDAG["dags/wing_b_score_dag.py<br/>scheduled scoring, every ~15 min"]
        SCORER["scoring/scorer.py<br/>GradientBoostingRegressor → alpha score"]
        ANALYST["dashboard/pages/5_signal_publisher.py<br/>human analyst UI"]
        PGSIG[("Postgres: analyst_signals<br/>audit trail")]
        WRITER["common/signal_writer.py<br/>validate + serialize to contract"]
        REDIS[["Redis PUBLISH<br/>'doorbell' notification"]]

        ETL --> SCOREDAG
        SCOREDAG --> SCORER
        SCORER --> WRITER
        ANALYST --> PGSIG
        ANALYST --> WRITER
        WRITER --> REDIS
    end

    S3[("S3 bucket / LocalStack<br/>signals/*.json — the contract")]
    WRITER -- "writes AlphaSignal JSON" --> S3

    subgraph WingA["WING A — Portfolio Construction & Execution"]
        INGEST["signal_ingestion/ingestor.py<br/>poll S3, validate, deserialize"]
        CONSTRUCT["portfolio/constructor.py<br/>vol-target sizing, delta calc"]
        RISK["risk/gate.py<br/>pre-trade risk checks"]
        OM["order_manager/manager.py<br/>TWAP slicing, ZMQ serialize"]
        ALPACA["gateway/alpaca_feed.py<br/>live market data, WS"]

        S3 -- "polls" --> INGEST
        ALPACA -. "live mid price" .-> CONSTRUCT
        ALPACA -. "live mid price" .-> RISK
        INGEST -- "AlphaSignal" --> CONSTRUCT
        CONSTRUCT -- "Order" --> RISK
        RISK -- "approved" --> OM
    end

    ZMQPUSH{{"ZeroMQ PUSH<br/>tcp://*:5557"}}
    OM --> ZMQPUSH

    subgraph CppCore["C++ EXECUTION CORE"]
        RECV["push_pull_receiver.hpp<br/>parse JSON → Order"]
        ENGINE["execution_engine.hpp<br/>route to symbol"]
        ROUTER["book_router.hpp<br/>sync flat + tree books"]
        BOOK["order_book.cpp<br/>match_market / match_limit"]
        FLAT["flat_order_book.hpp<br/>O(1) price discovery"]
        RM["risk_manager.hpp<br/>post-trade position/PnL"]
        PG["pg_pool.hpp<br/>persist trade"]
        LOG["logger.hpp<br/>structured async log"]

        ZMQPUSH --> RECV --> ENGINE --> ROUTER
        ROUTER --> BOOK
        ROUTER --> FLAT
        BOOK -- "on_trade callback" --> RM
        RM --> PG
        RM --> LOG
    end

    ZMQPUB{{"ZeroMQ PUB<br/>fill broadcast"}}
    PG --> ZMQPUB

    subgraph Return["FILL RETURN PATH"]
        FILLSUB["gateway/fill_subscriber.py<br/>ZMQ SUB"]
        PNL["pnl/tracker.py<br/>running PnL"]
        API["dashboard/api.py<br/>FastAPI + WebSocket"]
        REACT["React dashboard<br/>live fills, PnL, book"]

        ZMQPUB --> FILLSUB --> PNL --> API --> REACT
    end
```

**Reading the diagram left to right:** a signal is born in Wing B (model or analyst), lands in S3 in the shared contract format, gets polled by Wing A, is sized against a live Alpaca price into a concrete order, passes a Python risk gate, crosses ZeroMQ into the C++ core, gets matched against the live order book, updates risk and persists to Postgres, and broadcasts a fill back out through a second ZeroMQ socket to the dashboard — closing the loop in well under the width of a single market tick.

---

## Wing B: The Analytics & Signal Generation Core

Wing B owns all data processing and modeling. It blends **systematic, ML-driven signals** with **discretionary, human-driven signals** through the same publication path, so Wing A never needs to know or care which type of signal it's consuming — both arrive as the same `AlphaSignal` JSON.

### 1. ML-Driven Alpha Generation

`scoring/scorer.py` implements the systematic alpha model:

- **Model** — a `GradientBoostingRegressor` trained to predict a "fair" P/E ratio for each stock from its fundamentals (margins, growth, leverage, sector).
- **Signal construction** — the gap between predicted fair P/E and actual P/E ("upside") is cross-sectionally ranked across the entire universe and squashed into `[-1.0, +1.0]`. Cross-sectional ranking matters here: it keeps signal magnitudes comparable across stocks and market regimes, rather than letting one noisy raw valuation gap dominate sizing.
- **Output** — `scoring/scorer.py` hands every scored symbol to `common/signal_writer.py`, which is the single choke point all signals — model or human — must pass through before reaching Wing A.

### 2. Human-in-the-Loop: The Analyst Signal Bridge

`dashboard/pages/5_signal_publisher.py` is a Streamlit page that lets a human analyst inject a discretionary trade idea directly into the same execution path the ML model uses:

- **Input** — ticker, direction, conviction (mapped to `confidence`), and a written thesis.
- **Durable storage** — the thesis and metadata are written first to the `analyst_signals` table in Postgres, so every discretionary call has a permanent audit trail independent of whether it ever gets executed.
- **Instant notification** — the same `common/signal_writer.py` module then publishes it to S3 in the standard contract, and a Redis `PUBLISH` acts as a low-latency "doorbell" so Wing A doesn't have to wait for its next poll cycle to notice a high-conviction human call.

### 3. Orchestration with Airflow

Two DAGs keep Wing B's outputs fresh without manual intervention:

- **`dags/wing_b_etl_dag.py`** — runs daily pre-market (`0 6 * * 1-5`), pulling fresh fundamentals and price data so every downstream model has current inputs.
- **`dags/wing_b_score_dag.py`** — runs on a tight intraday cadence (every ~15 minutes) to re-score the universe via `scoring/scorer.py` and refresh the signal files Wing A reads. This cadence matters operationally: it's deliberately kept inside the ingestor's staleness window (see below) so a signal is never rejected for being too old before Wing A even sees it.

---

## The Signal Contract: Bridging Wing B and Wing A

Every producer — model or human — writes through `common/signal_writer.py`, which enforces one strict JSON schema. This contract *is* the API boundary between the two wings; neither side needs to know anything about the other's internals as long as this shape holds:

```json
{
  "symbol": "AAPL",
  "signal": 0.80,
  "confidence": 0.80,
  "target_qty": 400,
  "limit_price_bps": 0,
  "generated_at": "2026-07-06T13:23:48.740635Z"
}
```

| Field | Range / Type | Role |
|---|---|---|
| `symbol` | ticker string | Which instrument this idea concerns |
| `signal` | `[-1.0, +1.0]` | Direction (sign) and raw conviction of the alpha idea |
| `confidence` | `[0.0, 1.0]` | Trust in the call — scales position size, and gates the signal out entirely below a minimum threshold |
| `target_qty` | integer, shares | Wing B's suggested ceiling — Wing A's own vol-targeted size is capped at this, but rarely reaches it |
| `limit_price_bps` | integer, price ×10,000 | `0` means "no limit — execute at market"; any other value pins a limit price |
| `generated_at` | ISO-8601 UTC timestamp | Freshness check — Wing A rejects anything older than its staleness window |

## Wing A: The Portfolio & Execution Core

Wing A's job is to turn an abstract signal into a filled order, safely. It's split across five stages, each in its own file with a single responsibility.

### 1. Signal Ingestion

`signal_ingestion/ingestor.py` continuously polls the shared S3 prefix. Each new file is downloaded, validated against the contract above (schema, staleness, universe membership, minimum signal/confidence thresholds), and deserialized into an `AlphaSignal` object. Polling — rather than a push-based call — makes the system resilient to network partitions: if Wing A goes down, signals simply accumulate in S3 and are picked up on restart instead of being lost.

### 2. Portfolio Construction

`portfolio/constructor.py` is the brain of Wing A. Given a validated `AlphaSignal`, it:

1. **Checks current state** — pulls current position and the live Alpaca mid price for the symbol.
2. **Vol-targets the size** — `_vol_target_size()` scales position size inversely to the stock's realized volatility, so every idea gets an equivalent *risk* allocation rather than an equivalent *share count*. `confidence` then scales that size further, and the result is capped by `target_qty`.
3. **Computes the delta** — the difference between the newly sized target position and what's currently held is the actual quantity that gets ordered (so a repeated signal on an already-held position doesn't double up).
4. **Builds the `Order`** — direction from `sign(signal)`, order type from whether `limit_price_bps` is zero (market) or non-zero (limit).

### 3. Order Execution and the C++ Core

- **`risk/gate.py`** — the last Python-side checkpoint before an order can leave the process: order-size limits, position limits, and total notional exposure.
- **`order_manager/manager.py`** — applies TWAP slicing for large orders (splitting anything above a size threshold into timed child orders to reduce market impact and signalling), then serializes each order and sends it over a ZeroMQ **PUSH** socket to the C++ core.
- **`push_pull_receiver.hpp`** — the C++ side's ZMQ **PULL**, parsing the incoming JSON back into a native `Order` struct.
- **`execution_engine.hpp`** — routes the order to the correct per-symbol book.
- **`book_router.hpp`** — owns and keeps synchronized the two order-book representations: a `std::map`-based tree for full order lifecycle, and a flat array (`flat_order_book.hpp`) for O(1) best-bid/ask lookups used by price discovery and risk checks.
- **`order_book.cpp`** — runs the actual matching (`match_market` / `match_limit`), producing `Trade` objects as fills occur.

### 4. Risk Management

`risk_manager.hpp` updates position and realized/unrealized PnL atomically on every fill, independent of the matching thread — so risk state is always current even under concurrent order flow, without needing to lock the hot matching path.

### 5. Fills, PnL, and the Live Dashboard

Fills are persisted via `pg_pool.hpp` (TimescaleDB) and logged via `logger.hpp`, then broadcast on a ZeroMQ **PUB** socket. On the Python side, `gateway/fill_subscriber.py` consumes them, `pnl/tracker.py` folds each fill into running session PnL, and `dashboard/api.py` pushes the update over a FastAPI WebSocket to the React front end — so a fill is visible on the live dashboard within milliseconds of being matched.

---

## Local Development with Docker

The entire infrastructure runs locally with zero cloud dependencies, via `docker-compose.yml`:

- **`timescaledb`** — Postgres for trades, positions, and the `analyst_signals` audit table.
- **`localstack`** — a mock of AWS S3, so the signal-polling path can be exercised end-to-end without real AWS credentials.
- **`airflow`** — a full local Airflow deployment for running the ETL and scoring DAGs on schedule.

`wing-a/dev_server.py` additionally mocks the C++ engine and the Alpaca market data feed, so Wing A's Python components can be developed and tested in complete isolation from both the compiled engine and a live broker connection.

## Getting Started

### 1. Start Docker Services
```bash
# Starts LocalStack, PostgreSQL, and Airflow
docker-compose up -d
```

### 2. Initialize Databases and S3
```bash
# Create the database schema for Wing B
psql postgresql://quant:quant@localhost:5434/quantcore -f wing-b/db/schema.sql

# Create the S3 bucket for signals, pointed at LocalStack
aws --endpoint-url=http://localhost:4566 s3 mb s3://quantcore-signals
```

### 3. Run Wing B (Analytics)
```bash
cd wing-b
pip install -r requirements.txt

# Trigger DAGs via the Airflow UI at http://localhost:8080
#   -> wing_b_etl_dag
#   -> wing_b_score_dag
# ...or run the scorer directly for a one-off signal batch:
python -m scoring.scorer
```

### 4. Run Wing A (Execution)
```bash
# Terminal 1 — mock the C++ engine + external feeds
cd wing-a
python dev_server.py

# Terminal 2 — run the real Wing A pipeline against the mocks
cd wing-a
pip install -r requirements.txt
python main.py --dev
```

You should see Wing A ingesting signals from the S3 mock, sizing them in `portfolio/constructor.py`, and printing the orders it sends to the mocked C++ engine — the same path a real signal takes in production, just with synthetic data underneath.

## Design Decisions Worth Highlighting

A few choices in this system are deliberate engineering trade-offs, not defaults — worth calling out to a technical reviewer:

- **Two order-book representations, not one.** `order_book.cpp` (tree-based) handles correctness-critical order lifecycle; `flat_order_book.hpp` (array-based) exists purely so price-discovery reads never contend with the matching path — an O(1) lookup versus an O(log n) tree traversal on the hot read path.
- **Polling over push for signal ingestion.** A push-based handoff would be lower latency, but polling S3 makes Wing A resilient to being offline — signals queue up rather than get dropped.
- **Vol-targeting instead of naive `signal × constant` sizing.** Ensures each trade idea contributes comparable *risk*, not comparable *share count*, to the portfolio — a stock at 60% annualized vol gets proportionally fewer shares than one at 15%.
- **TWAP slicing gated by a size threshold.** Small orders (the common case here) go out as a single order with no added latency; only orders large enough to move the book get sliced — so the system doesn't pay a time cost it doesn't need.
