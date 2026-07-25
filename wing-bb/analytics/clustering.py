"""
clustering.py — group stocks by fundamental SIMILARITY, not by label.

THE FINANCE IDEA:
Two stocks in the same official sector can be totally different
animals — a cash-burning growth company vs a stable dividend payer.
Clustering ignores the labels and groups stocks by what their
NUMBERS look like. Stocks in the same cluster but priced very
differently are candidates for 'relative value' trades.

THE ML PIPELINE (3 steps, each explained inline):
    scale → PCA → K-Means
"""

import pandas as pd

# StandardScaler: puts every feature on the same scale.
# PCA: squashes many features down to 2 dimensions we can plot.
# KMeans: the actual grouping algorithm.
from sklearn.preprocessing import StandardScaler
from sklearn.decomposition import PCA
from sklearn.cluster import KMeans

from common.db import engine

# The fundamental features we cluster on.
FEATURES = ["pe_ratio", "rev_growth", "margin", "momentum_6m", "volatility"]


def cluster_stocks(n_clusters: int = 3) -> pd.DataFrame:
    """Cluster the whole universe into n_clusters groups.

    Returns the fundamentals table with three new columns:
        cluster  — which group the stock landed in (0, 1, 2, ...)
        pca_x, pca_y — 2-D coordinates for plotting the clusters
    """
    # Load one row per stock.
    df = pd.read_sql("SELECT * FROM sector_fundamentals", engine)

    # ── Step 1: SCALE the features ─────────────────────────────────
    # Why: P/E is ~8–45 while margin is ~0.03–0.35. Without scaling,
    # the distance calculation inside K-Means would be dominated by
    # P/E just because its NUMBERS are bigger — not because it
    # matters more. StandardScaler transforms every column to
    # mean 0, std-dev 1, so all features count equally.
    #
    # fit_transform = learn the column means/stds (fit), then apply
    # the transformation (transform), in one call.
    X = StandardScaler().fit_transform(df[FEATURES])

    # ── Step 2: PCA down to 2 dimensions ───────────────────────────
    # Why: K-Means distance gets unreliable in many dimensions
    # ('curse of dimensionality'), and humans can only see 2-D plots.
    # PCA finds the 2 directions that preserve the MOST variation in
    # the data and projects everything onto them.
    coords = PCA(n_components=2).fit_transform(X)

    # ── Step 3: K-Means clustering ─────────────────────────────────
    # The algorithm in plain words:
    #   1. drop n_clusters random 'centre' points into the space
    #   2. assign each stock to its nearest centre
    #   3. move each centre to the average of its assigned stocks
    #   4. repeat 2–3 until nothing moves
    # n_init=10 runs it 10 times from different random starts and
    # keeps the best — K-Means can get stuck in bad starts otherwise.
    # random_state pins the randomness so results are reproducible.
    km = KMeans(n_clusters=n_clusters, n_init=10, random_state=42)

    # fit_predict learns the clusters AND returns each row's label.
    df["cluster"] = km.fit_predict(coords)

    # Keep the 2-D coordinates so the dashboard can scatter-plot them.
    df["pca_x"] = coords[:, 0]   # first PCA direction
    df["pca_y"] = coords[:, 1]   # second PCA direction

    return df
