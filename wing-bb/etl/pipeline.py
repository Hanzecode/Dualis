"""
pipeline.py — the Polars ETL pipeline.

ETL = Extract, Transform, Load.
  Extract  → data_sources.py already did this (fetched raw prices)
  Transform→ THIS file: clean the data and engineer features
  Load     → write the result into the shared database + Parquet

Why Polars instead of pandas here?
Polars builds a QUERY PLAN first (lazy evaluation) and optimises the
whole plan before running it — like a database query planner. On big
data it's 5–20x faster than pandas. On our small demo data the speed
doesn't matter, but the PATTERN is what you're learning.
"""

import polars as pl              # the fast DataFrame library
import pandas as pd              # only used at the boundaries (in/out)

from common.db import engine, init_tables
from etl.data_sources import fetch_prices, fetch_fundamentals, fetch_macro


def clean_and_feature(prices_pd: pd.DataFrame) -> pl.DataFrame:
    """Clean raw prices and add the daily-return feature, in Polars.

    Takes the pandas frame from data_sources, returns a Polars frame.
    """
    # Convert pandas → Polars. From here on we're in Polars land.
    #
    # One real-world wrinkle first: our trade_date column holds Python
    # 'date' objects, which Polars can't convert without the pyarrow
    # library. Converting the column to pandas' native datetime type
    # makes it a 'simple' column Polars accepts directly. Lesson:
    # normalise your TYPES at system boundaries.
    prices_pd = prices_pd.copy()                       # don't mutate the caller's frame
    prices_pd["trade_date"] = pd.to_datetime(prices_pd["trade_date"])
    df = pl.from_pandas(prices_pd)

    # .lazy() switches to LAZY mode: nothing below executes yet —
    # Polars just records the plan. .collect() at the end runs it all
    # in one optimised pass.
    df = (
        df.lazy()

        # ── Step 1: basic data-quality filters ────────────────────
        # Prices must be positive; volume can't be negative.
        # In Polars you build expressions with pl.col("name").
        .filter(pl.col("close") > 0)
        .filter(pl.col("volume") >= 0)

        # ── Step 2: drop exact duplicate rows ──────────────────────
        # Real feeds sometimes deliver the same tick twice.
        .unique(subset=["trade_date", "ticker"])

        # ── Step 3: sort so time-based features are correct ───────
        # pct_change-style features NEED rows in date order per stock.
        .sort(["ticker", "trade_date"])

        # ── Step 4: engineer the daily-return feature ──────────────
        # .over("ticker") is the magic: it makes the calculation
        # restart for each stock, so AAPL's first day doesn't compute
        # a 'return' versus MSFT's last day.
        .with_columns(
            (pl.col("close").pct_change().over("ticker"))
            .alias("daily_ret")        # name the new column
        )

        # ── Step 5: the first day of each stock has no previous
        # close, so its return is null. Replace null with 0.0 ───────
        .with_columns(pl.col("daily_ret").fill_null(0.0))

        # NOW run the whole optimised plan.
        .collect()
    )
    return df


def load_to_db(prices: pl.DataFrame, fundamentals: pd.DataFrame) -> None:
    """Write the cleaned data into the shared database.

    This is the 'L' of ETL. We replace the tables wholesale each run —
    simple and fine for a daily batch job. (A production system might
    UPSERT only the new rows instead.)
    """
    # Make sure the tables exist before writing.
    init_tables()

    # Polars → pandas at the boundary, because pandas has the
    # convenient .to_sql() writer that works with SQLAlchemy.
    prices.to_pandas().to_sql(
        "sector_prices",       # table name
        engine,                # where to write (our shared engine)
        if_exists="replace",   # drop + recreate the table each run
        index=False,           # don't write the row numbers as a column
    )

    fundamentals.to_sql(
        "sector_fundamentals",
        engine,
        if_exists="replace",
        index=False,
    )


def load_macro(macro: pd.DataFrame) -> None:
    """Write the FRED (or synthetic) macro series to its table.
    Kept separate from load_to_db so a macro-API failure can never
    block the price load — independent failures stay independent."""
    macro.to_sql("macro_indicators", engine, if_exists="replace", index=False)


def run() -> None:
    """The full ETL job, start to finish. Called by run_etl.py
    locally, or by an Airflow task / Kubernetes CronJob in prod."""
    print("[etl] extracting prices...")
    raw = fetch_prices()                     # Extract

    print(f"[etl] transforming {len(raw):,} rows with Polars...")
    clean = clean_and_feature(raw)           # Transform

    print("[etl] deriving fundamentals...")
    funda = fetch_fundamentals(raw)           # more Extract/Transform

    print("[etl] loading into database...")
    load_to_db(clean, funda)                  # Load

    print("[etl] fetching macro indicators (FRED)...")
    macro = fetch_macro()                     # Extract (macro)
    load_macro(macro)                         # Load (macro)

    print(f"[etl] done — {clean.height:,} price rows, "
          f"{len(funda)} fundamentals rows, "
          f"{len(macro):,} macro observations written.")
    # .height is Polars for 'number of rows' (like len() in pandas).
