"""
valuation.py — estimate a 'fair' P/E for every stock, then flag
the ones trading far away from it.

THE FINANCE IDEA (study guide 1.2 & 2):
A stock's P/E ratio should be JUSTIFIED by its fundamentals —
faster growth and fatter margins deserve a higher P/E. We train a
model to learn that relationship across the whole universe, then
ask it: 'given THIS stock's fundamentals, what P/E would be
normal?' If the actual P/E is far below the model's estimate, the
stock may be cheap (and vice versa).

THE ML IDEA:
Gradient boosting (XGBoost family) = many small decision trees,
each one correcting the previous trees' mistakes. We use
scikit-learn's GradientBoostingRegressor — same algorithm family,
zero extra dependencies. SHAP then explains every prediction.
"""

import numpy as np
import pandas as pd

from sklearn.ensemble import GradientBoostingRegressor

from common.db import engine

# Features the model may use to justify a P/E.
# NOTE: pe_ratio itself is EXCLUDED — it's the thing we predict.
# Including it would be data leakage (study guide section 2.4).
FEATURES = ["rev_growth", "margin", "momentum_6m", "volatility"]


def train_and_score() -> tuple[pd.DataFrame, GradientBoostingRegressor]:
    """Train the fair-value model and score every stock.

    Returns:
        scored — fundamentals + fair_pe + upside columns
        model  — the trained model (the dashboard reuses it for SHAP)
    """
    df = pd.read_sql("SELECT * FROM sector_fundamentals", engine)

    # X = the inputs (features), y = the answer to learn (P/E).
    X = df[FEATURES]
    y = df["pe_ratio"]

    # Build the model. Each parameter explained:
    model = GradientBoostingRegressor(
        n_estimators=200,    # how many small trees to stack
        max_depth=2,         # each tree asks at most 2 questions —
                             # shallow trees resist overfitting on
                             # our small dataset
        learning_rate=0.05,  # how big a correction each tree makes;
                             # small steps + many trees = smoother fit
        random_state=42,     # reproducible randomness
    )

    # .fit() is where learning happens: the model studies X vs y.
    model.fit(X, y)

    # .predict() asks the trained model: 'what P/E SHOULD each stock
    # have, given its fundamentals?'
    df["fair_pe"] = model.predict(X)

    # Upside: how far actual price is from 'fair'.
    # If fair_pe is 20 and actual is 16, the stock could rise 25%
    # to reach fair value → upside = 20/16 - 1 = +0.25.
    df["upside"] = df["fair_pe"] / df["pe_ratio"] - 1

    # Sort so the 'cheapest vs model' stocks appear first.
    scored = df.sort_values("upside", ascending=False).reset_index(drop=True)
    return scored, model


def shap_explanation(model: GradientBoostingRegressor,
                     scored: pd.DataFrame,
                     ticker: str) -> pd.DataFrame:
    """Explain ONE stock's fair-value estimate, feature by feature.

    SHAP answers: 'how much did each feature push this prediction up
    or down, relative to the average stock?' — the itemised receipt
    from your study guide (section 2.6).
    """
    # Import here so the rest of the module works without shap installed.
    import shap

    # TreeExplainer is the fast exact SHAP algorithm for tree models.
    explainer = shap.TreeExplainer(model)

    # Pull out the single row for the requested ticker.
    # .loc[mask, cols] = select rows where mask is True.
    row = scored.loc[scored["ticker"] == ticker, FEATURES]

    # shap_values returns one number per feature for this row:
    # positive = pushed the fair P/E UP, negative = pushed it DOWN.
    values = explainer.shap_values(row)[0]      # [0] = first (only) row

    # Package into a tidy frame the dashboard can bar-chart.
    return (
        pd.DataFrame({
            "feature":    FEATURES,
            "shap_value": values,
        })
        # Sort by absolute impact so the biggest driver is on top.
        .assign(abs_impact=lambda d: d["shap_value"].abs())
        .sort_values("abs_impact", ascending=False)
        .drop(columns="abs_impact")
        .reset_index(drop=True)
    )
