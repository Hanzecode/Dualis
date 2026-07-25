"""
config.py — central settings for Wing B.

Why this file exists:
Every other file needs to know things like "where is the database?"
Instead of hardcoding those values in 10 different places, we keep
them in ONE place. Change it here, and the whole system updates.
"""

# 'os' lets us read environment variables — values passed in from
# outside the program (the terminal, Docker, or Kubernetes Secrets).
import os

# ── Database location ─────────────────────────────────────────────
# os.getenv("NAME", default) reads the environment variable NAME.
# If it doesn't exist, it falls back to the default after the comma.
#
# In production (Kubernetes), DATABASE_URL points at PostgreSQL:
#   postgresql://user:password@postgres-svc:5432/hedgefund
# For local learning, we fall back to SQLite — a tiny database that
# lives in a single file and needs zero setup. Same code works on both.
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///wing_b_local.db")

# ── Redis location (used to notify Wing A instantly) ─────────────
# Redis is optional in local dev — the code checks for it gracefully.
REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))  # int() because env vars are always strings

# ── FRED (Federal Reserve Economic Data) ──────────────────────────
# FRED is the St. Louis Fed's free macro-data service. You need a
# free API key from https://fred.stlouisfed.org/docs/api/api_key.html
# Set it as an environment variable:  export FRED_API_KEY=abc123
# If it's missing, the ETL generates synthetic macro series instead,
# so the project still runs without any signup.
FRED_API_KEY = os.getenv("FRED_API_KEY", "")

# Which FRED series we track. Keys are FRED's official series IDs;
# values are the human-readable names shown on the dashboard.
FRED_SERIES = {
    "DGS10":    "10Y Treasury yield (%)",   # benchmark interest rate
    "CPIAUCSL": "CPI (index)",              # inflation level
    "UNRATE":   "Unemployment rate (%)",    # labour market health
    "VIXCLS":   "VIX (volatility index)",   # market fear gauge
}

# ── The stock universe we analyse ─────────────────────────────────
# A dict mapping each sector name to the tickers we track inside it.
# Small on purpose: enough to demonstrate every feature without
# hammering free data APIs.
SECTOR_UNIVERSE = {
    "Technology":  ["AAPL", "MSFT", "NVDA", "GOOGL"],
    "Healthcare":  ["JNJ", "PFE", "UNH", "ABBV"],
    "Financials":  ["JPM", "BAC", "GS", "MS"],
    "Energy":      ["XOM", "CVX", "COP", "SLB"],
}

# ── How much history to fetch ─────────────────────────────────────
# 2 years of daily prices is enough to estimate factors and train
# the valuation model, while staying fast to download.
HISTORY_PERIOD = "2y"

# ── Where the ETL pipeline writes its output ──────────────────────
# Parquet is a fast, compressed, columnar file format — the standard
# for analytics data. Polars reads and writes it natively.
DATA_DIR = os.getenv("DATA_DIR", "data")
