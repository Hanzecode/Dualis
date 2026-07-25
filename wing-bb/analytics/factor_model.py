"""
factor_model.py — what is actually driving each sector's returns?

THE FINANCE IDEA (from your study guide, section 1 & 7):
A 'factor' is a systematic force that moves many stocks at once —
the overall market, the momentum effect, the value effect. Factor
analysis runs a REGRESSION of a sector's returns against those
forces to answer: "is this sector going up because of skill/news,
or just because it's riding the market factor?"

THE MATHS, in one line:
    sector_return = alpha + beta1*market + beta2*momentum + noise
The betas (slopes) are the EXPOSURES. Alpha (the intercept) is the
part no factor explains — the 'special sauce'.
"""

import numpy as np
import pandas as pd

# LinearRegression fits the classic least-squares line through data.
from sklearn.linear_model import LinearRegression

from common.db import engine


def _load_returns() -> pd.DataFrame:
    """Read daily returns from the DB and pivot to wide format.

    Wide format = one column per ticker, one row per date:
                    AAPL    MSFT    JPM ...
        2024-01-02  0.012  -0.004  0.001
    Regressions want this shape.
    """
    # read_sql runs a query and gives back a DataFrame.
    df = pd.read_sql(
        "SELECT trade_date, ticker, daily_ret FROM sector_prices",
        engine,
    )
    # pivot: rows=dates, columns=tickers, values=returns.
    return df.pivot(index="trade_date", columns="ticker", values="daily_ret")


def _build_factors(returns: pd.DataFrame) -> pd.DataFrame:
    """Construct simple factor return series from our own universe.

    Real funds buy factor data (Fama-French, Barra). We BUILD tiny
    versions from our own stocks so the project is self-contained:

    - market   : the average return of ALL stocks each day.
    - momentum : return of recent winners minus recent losers.
    """
    factors = pd.DataFrame(index=returns.index)

    # axis=1 means 'across columns', i.e. average over stocks per day.
    factors["market"] = returns.mean(axis=1)

    # Momentum factor, the classic 'winners minus losers' construction:
    # 1) total return of each stock over the lookback window
    lookback = min(126, len(returns) - 1)          # ~6 months, capped by data length
    past = (1 + returns).prod().pow(1)             # cumulative growth per stock
    past = (1 + returns.iloc[:lookback]).prod()    # growth over the early window only
    ranked = past.rank()                            # rank stocks 1..N by that growth

    # 2) winners = top third, losers = bottom third
    n = len(ranked)
    winners = ranked[ranked > 2 * n / 3].index      # ticker names of the top third
    losers  = ranked[ranked <= n / 3].index         # ticker names of the bottom third

    # 3) the factor's daily return = avg(winners) - avg(losers)
    factors["momentum"] = returns[winners].mean(axis=1) - returns[losers].mean(axis=1)

    return factors


def sector_exposures() -> pd.DataFrame:
    """For every sector, regress its daily return on the factors.

    Returns a table like:
        sector      | market_beta | momentum_beta | alpha_annual
        Technology  | 1.15        | 0.30          | 0.021
    Read as: 'Tech moves 1.15x the market, tilts toward momentum,
    and earned ~2.1%/year that the factors don't explain.'
    """
    returns = _load_returns()
    factors = _build_factors(returns)

    # Map each ticker to its sector (one query, then a dict lookup).
    meta = pd.read_sql("SELECT ticker, sector FROM sector_fundamentals", engine)
    sector_of = dict(zip(meta["ticker"], meta["sector"]))  # {'AAPL': 'Technology', ...}

    rows = []
    # set(...) collapses duplicates → unique sector names.
    for sector in sorted(set(sector_of.values())):
        # All tickers belonging to this sector AND present in returns.
        cols = [t for t in returns.columns if sector_of.get(t) == sector]
        if not cols:
            continue

        # The sector's return series = equal-weight average of members.
        y = returns[cols].mean(axis=1)

        # Align X and y and drop days with missing data — regression
        # cannot handle NaNs.
        data = pd.concat([y.rename("y"), factors], axis=1).dropna()

        # Fit:  y = alpha + b1*market + b2*momentum
        model = LinearRegression()
        model.fit(data[["market", "momentum"]], data["y"])

        rows.append({
            "sector":        sector,
            "market_beta":   round(float(model.coef_[0]), 3),  # slope on factor 1
            "momentum_beta": round(float(model.coef_[1]), 3),  # slope on factor 2
            # intercept is a DAILY alpha; ×252 trading days ≈ annual.
            "alpha_annual":  round(float(model.intercept_) * 252, 4),
        })

    return pd.DataFrame(rows)
