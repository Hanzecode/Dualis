"""
data_sources.py — fetches raw market data from the outside world.

This is the OUTERMOST layer of Wing B: the boundary between the
internet and your system. Everything downstream (ETL, models,
dashboard) consumes what this file brings in.

Design choice worth understanding:
We try to download REAL data via yfinance. If that fails, the default
is to raise — a real outage should be a loud, visible failure, not a
silent swap to fake data that lets the pipeline "succeed" on garbage.
Set ALLOW_SYNTHETIC_DATA=true (common/config.py) to opt back into the
old always-runs-on-fake-data behaviour for demos/CI.
"""

# Standard library imports first (Python convention).
import numpy as np            # fast numerical arrays — used for synthetic data
import pandas as pd           # DataFrames — yfinance returns these

# Our own settings.
from common.config import (
    SECTOR_UNIVERSE, HISTORY_PERIOD, FRED_API_KEY, FRED_SERIES,
    ALLOW_SYNTHETIC_DATA,
)


def fetch_prices() -> pd.DataFrame:
    """Return a long-format DataFrame of daily prices for every ticker.

    'Long format' means one row per (date, ticker) pair:
        trade_date | ticker | sector | close | volume
    This shape is what databases and Polars pipelines like best.
    """
    try:
        # Import inside the function so the project still works if
        # yfinance isn't installed — we only need it on this path.
        import yfinance as yf

        frames = []  # we'll collect one small DataFrame per ticker here

        # SECTOR_UNIVERSE is a dict: {"Technology": ["AAPL", ...], ...}
        # .items() gives us (sector_name, list_of_tickers) pairs.
        for sector, tickers in SECTOR_UNIVERSE.items():
            for ticker in tickers:
                # Ask Yahoo Finance for daily history. auto_adjust=True
                # folds dividends/splits into the price, which is what
                # you want for return calculations.
                hist = yf.Ticker(ticker).history(
                    period=HISTORY_PERIOD, auto_adjust=True
                )

                # Empty response = bad ticker or network issue. Skip it.
                if hist.empty:
                    continue

                # Build a tidy frame with exactly the columns we keep.
                frames.append(pd.DataFrame({
                    "trade_date": hist.index.date,     # index is a DatetimeIndex; .date strips the time part
                    "ticker":     ticker,
                    "sector":     sector,
                    "close":      hist["Close"].values,   # .values = plain numpy array, drops the index
                    "volume":     hist["Volume"].values,
                }))

        # If nothing downloaded, force the fallback path below.
        if not frames:
            raise RuntimeError("no data returned")

        # Stack all the per-ticker frames into one big one.
        # ignore_index=True renumbers rows 0..N cleanly.
        return pd.concat(frames, ignore_index=True)

    except Exception as exc:
        if not ALLOW_SYNTHETIC_DATA:
            raise RuntimeError(
                "fetch_prices(): live yfinance fetch failed and "
                "ALLOW_SYNTHETIC_DATA is not set — refusing to silently "
                "substitute fake price data. Fix the real data source, "
                "or set ALLOW_SYNTHETIC_DATA=true if this is a deliberate "
                "offline/demo run."
            ) from exc
        print(f"[data_sources] live fetch failed ({exc}); generating synthetic data "
              f"(ALLOW_SYNTHETIC_DATA=true)")
        return _synthetic_prices()


def _synthetic_prices() -> pd.DataFrame:
    """Generate fake-but-realistic price history.

    The leading underscore in the name is a Python convention meaning
    'private — for use inside this module only'.

    How the fake prices work: a RANDOM WALK. Each day's return is a
    small random number, and the price is the running product of
    (1 + return). This mimics how real prices wander.
    """
    # A fixed seed makes the randomness REPRODUCIBLE: every run
    # generates the exact same data, which makes debugging sane.
    rng = np.random.default_rng(seed=42)

    # ~2 years of weekdays. freq="B" means business days (no weekends).
    dates = pd.bdate_range(end=pd.Timestamp.today(), periods=504)

    frames = []
    for sector, tickers in SECTOR_UNIVERSE.items():
        for ticker in tickers:
            # Daily returns: average +0.03%/day, std-dev 2% — roughly
            # equity-like. size=len(dates) gives one return per day.
            rets = rng.normal(loc=0.0003, scale=0.02, size=len(dates))

            # Start near $100 and compound the returns:
            # price_t = 100 * (1+r1) * (1+r2) * ... — cumprod does this.
            prices = 100 * np.cumprod(1 + rets)

            # Volume: log-normal gives realistic 'mostly small,
            # occasionally huge' trading volumes.
            volume = rng.lognormal(mean=15, sigma=0.4, size=len(dates)).astype(int)

            frames.append(pd.DataFrame({
                "trade_date": dates.date,
                "ticker":     ticker,
                "sector":     sector,
                "close":      prices,
                "volume":     volume,
            }))

    return pd.concat(frames, ignore_index=True)


def fetch_fundamentals(prices: pd.DataFrame) -> pd.DataFrame:
    """Build a one-row-per-ticker fundamentals table.

    UPGRADED: we now try to pull REAL fundamentals from yfinance's
    .info dictionary (trailing P/E, profit margin, revenue growth).
    Yahoo's data is patchy — fields are sometimes missing or None —
    so the fallback is PER FIELD: each missing value is filled with
    a plausible synthetic one, and we record which source was used.

    Momentum and volatility are always computed genuinely from the
    price history we already hold — no API needed for those.
    """
    rng = np.random.default_rng(seed=7)

    # Try to import yfinance ONCE, outside the loop. If it's not
    # installed, 'yf' stays None and every field uses the fallback.
    try:
        import yfinance as yf
    except ImportError:
        yf = None

    rows = []
    for ticker, grp in prices.groupby("ticker"):
        # Always sort by date before doing time calculations.
        grp = grp.sort_values("trade_date")

        # ── Genuinely derived from prices (always real) ───────────
        daily_ret = grp["close"].pct_change()
        momentum = grp["close"].iloc[-1] / grp["close"].iloc[-126] - 1

        # ── Real fundamentals attempt ─────────────────────────────
        # info is a plain dict like {'trailingPE': 28.3, ...}.
        # .get(key) returns None when the key is absent — no crash.
        info = {}
        if yf is not None:
            try:
                info = yf.Ticker(ticker).info
            except Exception:        # network error, rate limit, etc.
                info = {}            # empty dict → all fields fall back

        # Helper: take the real value if it exists and is sane,
        # otherwise the synthetic fallback. Returning a (value,
        # source) pair lets us record where each number came from.
        def field(real, fallback, lo, hi):
            # 'real is not None' guards missing keys; the lo/hi check
            # guards garbage values (Yahoo sometimes returns absurd
            # numbers like a P/E of 4000 for near-zero-earnings firms).
            if real is not None and lo <= real <= hi:
                return float(real), "yahoo"
            return float(fallback), "synthetic"

        pe, pe_src = field(
            info.get("trailingPE"),            # real: price / trailing EPS
            rng.uniform(8, 45), 1, 200,
        )
        margin, mg_src = field(
            info.get("profitMargins"),          # real: net income / revenue
            rng.uniform(0.03, 0.35), -1, 1,
        )
        growth, gr_src = field(
            info.get("revenueGrowth"),          # real: YoY revenue change
            rng.uniform(-0.05, 0.30), -1, 5,
        )

        rows.append({
            "ticker":      ticker,
            "sector":      grp["sector"].iloc[0],
            "pe_ratio":    pe,
            "rev_growth":  growth,
            "margin":      margin,
            "momentum_6m": float(momentum),
            "volatility":  float(daily_ret.std()),
            # data_source records provenance, e.g. 'yahoo' or
            # 'synthetic' or 'mixed' — honesty about data quality is
            # itself a feature analysts (and interviewers) care about.
            "data_source": (
                "yahoo" if {pe_src, mg_src, gr_src} == {"yahoo"}
                else "synthetic" if {pe_src, mg_src, gr_src} == {"synthetic"}
                else "mixed"
            ),
        })

    return pd.DataFrame(rows)


def fetch_macro() -> pd.DataFrame:
    """Fetch macroeconomic series from FRED (or synthesise them).

    Returns long format: obs_date | series_id | series_name | value.

    FRED needs a free API key (see config.py). With no key, or any
    network failure, this raises unless ALLOW_SYNTHETIC_DATA is set —
    see the module docstring for why silent fallback is off by default.
    """
    if not FRED_API_KEY:
        if not ALLOW_SYNTHETIC_DATA:
            raise RuntimeError(
                "fetch_macro(): FRED_API_KEY is not set — refusing to "
                "silently substitute fake macro data. Set FRED_API_KEY "
                "(free key at fred.stlouisfed.org), or ALLOW_SYNTHETIC_DATA=true "
                "if this is a deliberate offline/demo run."
            )
    else:
        try:
            # fredapi wraps FRED's REST API in one friendly class.
            from fredapi import Fred
            fred = Fred(api_key=FRED_API_KEY)

            frames = []
            for series_id, name in FRED_SERIES.items():
                # get_series returns a pandas Series indexed by date.
                # observation_start trims to ~2 years to match prices.
                s = fred.get_series(series_id, observation_start="2024-06-01")
                frames.append(pd.DataFrame({
                    "obs_date":    s.index.date,
                    "series_id":   series_id,
                    "series_name": name,
                    "value":       s.values,
                }))
            df = pd.concat(frames, ignore_index=True)
            # FRED uses NaN for market holidays — drop those rows.
            return df.dropna(subset=["value"])

        except Exception as exc:
            if not ALLOW_SYNTHETIC_DATA:
                raise RuntimeError(
                    "fetch_macro(): live FRED fetch failed and "
                    "ALLOW_SYNTHETIC_DATA is not set — refusing to silently "
                    "substitute fake macro data."
                ) from exc
            print(f"[data_sources] FRED fetch failed ({exc}); using synthetic macro "
                  f"(ALLOW_SYNTHETIC_DATA=true)")

    # ── Synthetic fallback (ALLOW_SYNTHETIC_DATA=true only) ─────────
    rng = np.random.default_rng(seed=11)
    dates = pd.bdate_range(end=pd.Timestamp.today(), periods=504)

    # Per-series (start_level, daily_drift, daily_noise) tuned to
    # look like the real thing at a glance.
    params = {
        "DGS10":    (4.2,   0.0,    0.04),   # yields wiggle around a level
        "CPIAUCSL": (310.0, 0.03,   0.05),   # CPI drifts slowly upward
        "UNRATE":   (4.0,   0.0,    0.02),
        "VIXCLS":   (16.0,  0.0,    0.60),   # VIX is jumpy
    }

    frames = []
    for series_id, name in FRED_SERIES.items():
        start, drift, noise = params[series_id]
        # cumsum of (drift + noise) = a drifting random walk.
        values = start + np.cumsum(drift + rng.normal(0, noise, len(dates)))
        frames.append(pd.DataFrame({
            "obs_date":    dates.date,
            "series_id":   series_id,
            "series_name": name,
            "value":       values,
        }))
    return pd.concat(frames, ignore_index=True)
