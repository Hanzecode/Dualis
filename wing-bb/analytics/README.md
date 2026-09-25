# QuantCore Trading Engine

QuantCore is a high-performance, dual-service trading engine designed for algorithmic strategy execution. It combines a multithreaded C++ core for low-latency order matching with a Python ML pipeline for sophisticated alpha signal generation and order management. The system is architected for speed, correctness, and modularity, allowing components to be developed and deployed independently.

## Table of Contents
- [Architecture Overview](#architecture-overview)
- [Key Features](#key-features)
  - [Dual Order-Book Design](#1-dual-order-book-design)
  - [Pre-Trade Risk Management](#2-pre-trade-risk-management)
  - [ML-Driven Alpha Generation](#3-ml-driven-alpha-generation)
  - [TWAP Order Execution](#4-twap-order-execution)
- [Local Development Environment](#local-development-environment)
- [Build and Run](#build-and-run)

## Architecture Overview

The system is split into two primary services, decoupled via ZeroMQ to ensure high throughput and low-latency communication without creating a monolithic application.

*   **Wing A (C++ Execution Core)**: A multithreaded application responsible for order matching, risk management, and market data processing. It's built for maximum performance and direct control over the order lifecycle.
*   **Wing B (Python Analytics & Order Management)**: A suite of Python services for machine learning, data analysis, and high-level order management. It generates trading signals, manages parent order slicing (e.g., TWAP), and tracks portfolio PnL.

The communication between them follows a clear, asynchronous pattern:

```
┌───────────────────────────┐      ┌───────────────────────────┐
│  Python (Wing B)          │      │  C++ (Wing A)             │
│                           │      │                           │
│  [ OrderManager ]         │ PUSH │  [ PushPullReceiver ]     │
│  - Slices large orders    ├─────►│  - Receives order slices  │
│  - Sends child orders     │      │                           │
│                           │      │                           │
│  [ FillSubscriber ]       │      │  [ ExecutionEngine ]      │
│  - Receives fills         │◄─────┤  - Publishes fills        │
│  - Updates PnL            │  SUB │                           │
└───────────────────────────┘      └───────────────────────────┘
```

*   **Orders (PUSH/PULL)**: The Python `OrderManager` sends child order slices to the C++ engine over a `tcp://localhost:5557` PUSH/PULL socket. This is a reliable, load-balancing pattern for distributing work.
*   **Fills (PUB/SUB)**: The C++ engine publishes executed trades (fills) on a `tcp://*:5556` PUB/SUB socket. The Python `FillSubscriber` and other services (like a live dashboard) can subscribe to this feed to receive real-time updates.
*   **Signals (PUB/SUB)**: A separate Python process can publish alpha signals, which the C++ engine subscribes to, creating a path for ML models to drive trading decisions directly.

This decoupling allows the C++ engine to be restarted for a configuration change without stopping the Python ML services, and vice-versa.

---

## Key Features

### 1. Dual Order-Book Design

To achieve both extreme speed for price discovery and correctness for order lifecycle management, QuantCore uses a hybrid order book model for each symbol, orchestrated by the `BookRouter`.

#### `FlatOrderBook`: The Fast Path (O(1) Lookup)
*   **What it is**: A cache-aligned `std::array` that maps price levels directly to array indices.
*   **Performance**: Price lookups take **~10-40ns** by avoiding pointer chasing, fitting its 3.1MB footprint into L3 cache.
*   **Use Case**: All price discovery queries (`best_bid`, `best_ask`, `mid_price`, `spread`). The trading strategy needs the fastest possible view of the market state.
*   **Mechanism**: A `±$20.00` price window (400,000 price levels) is centered around a base price. A price is converted to an index via simple arithmetic: `slot = (price_bps - base_price_bps) + HALF_RANGE`. If the market moves outside this window, the book is recentered.

#### `OrderBook`: The Correctness Path (O(log n) Lookup)
*   **What it is**: A `std::map`-based order book that tracks every individual order.
*   **Performance**: Lookups are O(log n), which is slower but provides necessary structure.
*   **Use Case**: Order lifecycle management. It handles partial fills, cancellations, and maintains price-time priority using a `std::queue` of order IDs at each price level.
*   **Mechanism**: Bids are stored in a `std::map` sorted high-to-low, and asks are sorted low-to-high. A separate `std::unordered_map` provides O(1) access to any order by its ID for fast cancellations.

The `BookRouter` ensures these two books remain synchronized. New orders are submitted to the `OrderBook`, and when a trade occurs, the filled quantity is mirrored back to the `FlatOrderBook` to keep its depth view accurate.

### 2. Pre-Trade Risk Management

Before any order reaches the matching engine, it must pass through a synchronous, pre-trade risk gate (`RiskManager`). This prevents rogue algorithms or "fat-finger" errors from causing catastrophic losses. If a check fails, the order is rejected immediately.

The `RiskManager` enforces a set of configurable limits:

| Limit Type | Default Value | Description |
| :--- | :--- | :--- |
| **Max Position** | 10,000 shares | Maximum net long or short position per symbol. |
| **Max Order Size** | 1,000 shares | A single order cannot exceed this quantity. |
| **Max Notional** | $50,000 | Total market value across all positions. |
| **Max Concentration**| 40% | A single position cannot exceed this % of portfolio. |
| **Max Daily Loss** | -$1,000 | If realized PnL drops below this, all trading is halted. |
| **Rate Limit** | 50 orders/sec | Throttles order submission rate to prevent runaway loops. |

After a fill, the `RiskManager` is notified via a callback to update its internal state, tracking net position, average cost, and realized/unrealized PnL for every symbol.

### 3. ML-Driven Alpha Generation

The Wing B analytics pipeline uses machine learning to identify trading opportunities. The models are trained on fundamental and market data, and their insights are used to generate signals.

*   **Valuation Model**: A `GradientBoostingRegressor` model (from `scikit-learn` with 200 estimators) is trained to predict a "fair" P/E ratio for a stock based on its growth, margin, and momentum. Stocks trading at a significant discount to their predicted fair P/E are flagged as potential "buy" opportunities.

This runs on a loop (`while true; do python run_score.py; sleep 30; done`), and the resulting signals are written to a local signal directory, where Wing A polls and consumes them.

### 4. TWAP Order Execution

To minimize market impact when executing large orders, the Python `OrderManager` implements a Time-Weighted Average Price (TWAP) algorithm.

*   **Slicing**: Any parent order with a quantity greater than **500 shares** is automatically split into smaller child orders.
*   **Pacing**: Child orders are sent out sequentially every **30 seconds**.
*   **State Management**: Each parent order is tracked through a 7-state lifecycle (`NEW`, `SENT`, `OPEN`, `PARTIALLY_FILLED`, `FILLED`, `CANCELLED`, `REJECTED`), providing a clear view of its execution status.
*   **Resilience**: The ZMQ PUSH socket uses `NOBLOCK` and a retry loop with exponential backoff to handle cases where the C++ engine is temporarily unavailable, preventing data loss.

This logic runs in a background thread in the Python service, ensuring the main application remains responsive.

---

## Local Development Environment

The entire infrastructure can be run locally without any cloud dependencies, thanks to a fully containerized stack defined in `docker-compose.yml`. This provides a consistent, reproducible environment for development and integration testing.

The stack includes:
*   **`timescaledb`**: A PostgreSQL database with the TimescaleDB extension, used by the C++ engine for persisting trades and position data.
*   **`rds_mock`**: A plain PostgreSQL database that mimics the analytics DB, used by the Python services.
*   **`localstack`**: A mock of AWS S3, allowing the signal file polling mechanism to be tested without needing real AWS credentials or infrastructure.

### How to use it:
1.  **Start services**:
    ```bash
    docker-compose up -d
    ```
2.  **Initialize database schema**:
    ```bash
    psql postgresql://quant:quant@localhost:5434/quantcore -f db/schema.sql
    ```
3.  **Create S3 bucket in LocalStack**:
    ```bash
    aws --endpoint-url=http://localhost:4566 s3 mb s3://quantcore-signals
    ```

---

## Build and Run

### C++ Engine (Wing A)

The C++ application uses CMake.

1.  **Configure the build (Debug)**:
    ```bash
    mkdir build && cd build
    cmake ..
    ```

2.  **Configure the build (Release for performance)**:
    ```bash
    # -O3 optimizations are critical for FlatOrderBook performance
    mkdir build && cd build
    cmake .. -DCMAKE_BUILD_TYPE=Release
    ```

3.  **Compile**:
    ```bash
    cmake --build .
    ```

4.  **Run Tests**:
    ```bash
    ctest --output-on-failure
    ```

5.  **Run the Engine**:
    ```bash
    ./quantcore
    ```

### Python Services (Wing B)

1.  **Install dependencies**:
    ```bash
    pip install -r requirements.txt
    ```

2.  **Set environment variables**:
    Create a `.env` file in the project root with your API keys and any endpoint overrides. The `config/settings.py` will load it automatically.
    ```env
    # .env
    ALPACA_API_KEY="YOUR_KEY"
    ALPACA_SECRET_KEY="YOUR_SECRET"
    ```

3.  **Run the main application**:
    ```bash
    python main.py
    ```