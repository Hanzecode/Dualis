"""
db.py — the shared database layer.

This is the SINGLE most important file for understanding how
Wing B connects to Wing A: both wings read and write the SAME
database. The 'analyst_signals' table defined here is the bridge —
Wing B writes analyst trade ideas into it, and Wing A's alpha
model reads them out.
"""

# SQLAlchemy is Python's standard database toolkit. It lets the same
# code talk to SQLite (local dev) and PostgreSQL (production).
from sqlalchemy import (
    create_engine,   # opens a connection pool to the database
    text,            # lets us run raw SQL strings safely
)

# Import our central settings so the DB location lives in one place.
from common.config import DATABASE_URL

# ── The engine ─────────────────────────────────────────────────────
# An "engine" manages a pool of database connections. You create it
# ONCE when the program starts, then borrow connections from it.
# (Opening a fresh connection per query is slow — pooling reuses them.)
#
# future=True opts into SQLAlchemy's modern 2.0-style API.
engine = create_engine(DATABASE_URL, future=True)


def init_tables() -> None:
    """Create every table Wing B needs, if they don't already exist.

    'IF NOT EXISTS' makes this safe to run repeatedly — the second
    run simply does nothing. This property is called IDEMPOTENCE and
    it's the same idea your Ansible playbooks rely on.
    """
    # engine.begin() opens a connection AND a transaction.
    # The 'with' block guarantees: commit on success, rollback on
    # error, and the connection is always returned to the pool.
    # (This is the context-manager pattern from your study guide.)
    with engine.begin() as conn:

        # Table 1: cleaned daily prices, written by the ETL pipeline.
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS sector_prices (
                trade_date  DATE,            -- which day this row is for
                ticker      VARCHAR(10),     -- stock symbol, e.g. 'AAPL'
                sector      VARCHAR(30),     -- which sector it belongs to
                close       FLOAT,           -- closing price that day
                volume      BIGINT,          -- shares traded that day
                daily_ret   FLOAT,           -- % change vs previous close
                PRIMARY KEY (trade_date, ticker)  -- one row per stock per day
            )
        """))

        # Table 2: per-stock fundamentals snapshot for the screener
        # and the valuation model.
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS sector_fundamentals (
                ticker        VARCHAR(10) PRIMARY KEY,
                sector        VARCHAR(30),
                pe_ratio      FLOAT,   -- price / earnings: how expensive
                rev_growth    FLOAT,   -- revenue growth, year over year
                margin        FLOAT,   -- profit margin (profit / revenue)
                momentum_6m   FLOAT,   -- 6-month price return
                volatility    FLOAT    -- std-dev of daily returns (risk)
            )
        """))

        # Table 4: macroeconomic indicators from FRED.
        # Long format — one row per (date, series). The dashboard's
        # macro panel reads the latest value of each series.
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS macro_indicators (
                obs_date    DATE,            -- observation date
                series_id   VARCHAR(20),     -- FRED series code, e.g. 'DGS10'
                series_name VARCHAR(60),     -- human-readable label
                value       FLOAT,           -- the reading on that date
                PRIMARY KEY (obs_date, series_id)
            )
        """))

        # Table 3: THE BRIDGE TO WING A.
        # When an analyst publishes an idea from Streamlit, one row
        # lands here. Wing A's alpha model SELECTs from this table on
        # every signal-compute cycle and blends the analyst view into
        # its systematic score (the 'quantamental' loop).
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS analyst_signals (
                id          INTEGER PRIMARY KEY,  -- auto row id
                created_at  TIMESTAMP,            -- when it was published
                ticker      VARCHAR(10),          -- which stock
                direction   VARCHAR(5),           -- 'long' or 'short'
                conviction  INTEGER,              -- 1 (weak) to 5 (strong)
                thesis      TEXT,                 -- the analyst's reasoning
                expires_at  TIMESTAMP             -- ignore after this date
            )
        """))
