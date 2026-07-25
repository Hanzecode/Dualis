-- wing_a/db/schema.sql
-- ─────────────────────────────────────────────────────────────────────────────
--  WING A DATABASE SCHEMA
--  Target: TimescaleDB (PostgreSQL extension)
--  Run once at setup: psql -U quant -d quantcore -f schema.sql
--
--  Three tables:
--    trades     — every fill from the C++ matching engine (time-series)
--    positions  — current net position per symbol (upserted on each fill)
--    pnl        — rolling PnL snapshots for dashboard history
-- ─────────────────────────────────────────────────────────────────────────────

-- Enable TimescaleDB extension (must be installed on the PostgreSQL server)
CREATE EXTENSION IF NOT EXISTS timescaledb;

-- ─────────────────────────────────────────────────────────────────────────────
--  TRADES TABLE
--  One row per fill from the C++ engine.
--  trade_id matches the C++ Trade.trade_id — globally unique.
--  executed_at is the primary time column for TimescaleDB hypertable partitioning.
-- ─────────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS trades (
    trade_id          BIGINT          NOT NULL,       -- C++ engine trade ID
    symbol            VARCHAR(10)     NOT NULL,       -- e.g. 'AAPL'
    price_bps         BIGINT          NOT NULL,       -- fill price in basis points
    quantity          INTEGER         NOT NULL CHECK (quantity > 0),
    maker_order_id    BIGINT,                         -- resting order that was hit
    taker_order_id    BIGINT,                         -- incoming order that hit
    realised_pnl_usd  NUMERIC(12, 4)  DEFAULT 0,     -- PnL locked in by this fill
    executed_at       TIMESTAMPTZ     NOT NULL DEFAULT NOW(),

    CONSTRAINT trades_pkey PRIMARY KEY (trade_id, executed_at)
    -- Composite PK required by TimescaleDB hypertable (must include time column)
);

-- Convert trades to a TimescaleDB hypertable partitioned by executed_at
-- chunk_time_interval: one chunk per day — efficient for daily range queries
SELECT create_hypertable(
    'trades',
    'executed_at',
    chunk_time_interval => INTERVAL '1 day',
    if_not_exists => TRUE
);

-- Index on symbol for per-symbol queries (e.g. "all AAPL fills today")
CREATE INDEX IF NOT EXISTS idx_trades_symbol_time
    ON trades (symbol, executed_at DESC);

-- ─────────────────────────────────────────────────────────────────────────────
--  POSITIONS TABLE
--  One row per symbol — upserted (INSERT ... ON CONFLICT DO UPDATE) after each fill.
--  Tracks the live net position and cost basis.
-- ─────────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS positions (
    symbol              VARCHAR(10)     NOT NULL PRIMARY KEY,
    net_quantity        INTEGER         NOT NULL DEFAULT 0,
        -- positive = long, negative = short
    avg_cost_bps        BIGINT          NOT NULL DEFAULT 0,
        -- volume-weighted average cost in basis points
    realised_pnl_bps    BIGINT          NOT NULL DEFAULT 0,
        -- cumulative realised PnL in basis points
    unrealised_pnl_bps  BIGINT          NOT NULL DEFAULT 0,
        -- mark-to-market PnL in basis points (updated by PnLTracker on price tick)
    updated_at          TIMESTAMPTZ     NOT NULL DEFAULT NOW()
);

-- Seed the positions table with zero positions for all symbols
-- (safe to re-run — INSERT ... ON CONFLICT DO NOTHING)
INSERT INTO positions (symbol, net_quantity, avg_cost_bps)
VALUES
    ('AAPL', 0, 0),
    ('MSFT', 0, 0),
    ('TSLA', 0, 0),
    ('NVDA', 0, 0)
ON CONFLICT (symbol) DO NOTHING;

-- ─────────────────────────────────────────────────────────────────────────────
--  PNL SNAPSHOTS TABLE
--  Periodic PnL snapshots for charting history in the dashboard.
--  Inserted every minute by a background task in PnLTracker.
-- ─────────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS pnl_snapshots (
    symbol              VARCHAR(10)     NOT NULL,
    realised_pnl_usd    NUMERIC(12, 4)  NOT NULL,
    unrealised_pnl_usd  NUMERIC(12, 4)  NOT NULL,
    total_pnl_usd       NUMERIC(12, 4)  NOT NULL,
    net_quantity        INTEGER         NOT NULL,
    mid_price_usd       NUMERIC(12, 4),             -- market price at snapshot time
    snapshot_at         TIMESTAMPTZ     NOT NULL DEFAULT NOW()
);

SELECT create_hypertable(
    'pnl_snapshots',
    'snapshot_at',
    chunk_time_interval => INTERVAL '1 day',
    if_not_exists => TRUE
);

CREATE INDEX IF NOT EXISTS idx_pnl_symbol_time
    ON pnl_snapshots (symbol, snapshot_at DESC);

-- ─────────────────────────────────────────────────────────────────────────────
--  CONTINUOUS AGGREGATE — hourly PnL summary
--  TimescaleDB pre-computes this incrementally.
--  Query: SELECT * FROM pnl_hourly WHERE symbol = 'AAPL' ORDER BY bucket DESC
-- ─────────────────────────────────────────────────────────────────────────────

CREATE MATERIALIZED VIEW IF NOT EXISTS pnl_hourly
WITH (timescaledb.continuous) AS
SELECT
    time_bucket('1 hour', snapshot_at)  AS bucket,
    symbol,
    LAST(total_pnl_usd, snapshot_at)    AS pnl_at_hour_end,
    MAX(total_pnl_usd)                  AS pnl_high,
    MIN(total_pnl_usd)                  AS pnl_low,
    COUNT(*)                            AS snapshot_count
FROM pnl_snapshots
GROUP BY bucket, symbol
WITH NO DATA;

-- Refresh policy: update the hourly aggregate every 30 minutes
SELECT add_continuous_aggregate_policy(
    'pnl_hourly',
    start_offset => INTERVAL '3 hours',
    end_offset   => INTERVAL '1 minute',
    schedule_interval => INTERVAL '30 minutes',
    if_not_exists => TRUE
);

-- ─────────────────────────────────────────────────────────────────────────────
--  DATA RETENTION POLICY
--  Keep raw trade data for 1 year, PnL snapshots for 90 days.
--  Older data auto-dropped by TimescaleDB background job.
-- ─────────────────────────────────────────────────────────────────────────────

SELECT add_retention_policy(
    'trades',
    INTERVAL '1 year',
    if_not_exists => TRUE
);

SELECT add_retention_policy(
    'pnl_snapshots',
    INTERVAL '90 days',
    if_not_exists => TRUE
);

-- ─────────────────────────────────────────────────────────────────────────────
--  COMPRESSION POLICY
--  Compress chunks older than 7 days — reduces storage by ~90%.
-- ─────────────────────────────────────────────────────────────────────────────

ALTER TABLE trades        SET (timescaledb.compress, timescaledb.compress_segmentby = 'symbol');
ALTER TABLE pnl_snapshots SET (timescaledb.compress, timescaledb.compress_segmentby = 'symbol');

SELECT add_compression_policy('trades',        INTERVAL '7 days', if_not_exists => TRUE);
SELECT add_compression_policy('pnl_snapshots', INTERVAL '7 days', if_not_exists => TRUE);
