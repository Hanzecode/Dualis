"""
scoring/scorer.py — the ML SCORE step that turns Wing B's models into
systematic signals, written to S3 in Wing A's ingestor contract.

This is the 'score DAG' Wing A's ingestor.py comment refers to. It
reuses the valuation model you already built (analytics/valuation.py):
that model estimates a 'fair' P/E for each stock and an 'upside' (how
far the stock trades from fair value). We turn that upside into a
normalised alpha signal in [-1, +1].

Flow:  valuation model -> normalise upside -> contract dict -> S3
Same S3 prefix as analyst signals, so Wing A's ONE ingestor handles both.
"""


from analytics.valuation import train_and_score
from common.signal_writer import build_signal_dict, write_signals


# How aggressively to map 'upside' to a signal. tanh squashes any
# real number into (-1, 1) smoothly: small upside -> small signal,
# large upside -> saturates near +/-1. The multiplier sets the slope.
UPSIDE_TO_SIGNAL_GAIN = 2.0

# Base position size for a full-strength (|signal| = 1) conviction.
# A weaker signal scales this down proportionally.
BASE_QTY = 500


def score_universe() -> list[dict]:
    """Run the valuation model and produce one contract-format signal
    dict per stock. Pure computation — writing happens separately."""
    # train_and_score() returns (scored_df, model). scored_df has:
    # ticker, sector, pe_ratio, fair_pe, upside, ... (built earlier)
    scored, _model = train_and_score()

    # ── Cross-sectional ranking -> signal ─────────────────────────
    # Raw 'upside' magnitudes are model- and data-dependent and can be
    # tiny. Real quant signals are almost always CROSS-SECTIONAL: rank
    # each stock against its peers, so the best names get strong +
    # signals and the worst get strong - signals, spanning [-1, +1]
    # regardless of the raw upside scale. This also makes the signal
    # robust to the absolute level of the model's predictions.
    #
    # rank(pct=True) gives each stock its percentile in [0, 1];
    # mapping [0,1] -> [-1,+1] via (pct * 2 - 1) centres it so the
    # median stock is ~0, the cheapest ~+1, the most expensive ~-1.
    pct = scored["upside"].rank(pct=True)
    scored["signal"] = pct * 2.0 - 1.0

    signals = []
    for _, r in scored.iterrows():
        signal = float(r["signal"])

        # ── signal strength -> confidence ─────────────────────────
        # The further from the median (|signal| near 1), the more
        # conviction. Scaled into [0.30, 0.95] so even mid-rank names
        # carry a usable confidence and top names approach 0.95.
        confidence = float(0.30 + 0.65 * abs(signal))

        # ── signal strength -> position size ──────────────────────
        # |signal| = 1 -> BASE_QTY shares; scaled down for weaker views.
        # max(...,1) guarantees target_qty > 0 (the ingestor rejects <= 0).
        target_qty = max(1, int(abs(signal) * BASE_QTY))

        signals.append(build_signal_dict(
            symbol=r["ticker"],
            signal=signal,
            confidence=confidence,
            target_qty=target_qty,
            limit_price_bps=0,               # market order for simplicity
        ))

    return signals


def run() -> None:
    """Score the universe and write every signal to S3 under the
    'model/' source folder. This is what an Airflow task / CronJob
    calls on schedule."""
    print("[scorer] running valuation model...")
    signals = score_universe()

    print(f"[scorer] writing {len(signals)} model signals to S3...")
    paths = write_signals(signals, source="model")

    print(f"[scorer] done — {len(paths)} signals written.")
    for p in paths[:3]:
        print(f"  e.g. {p}")
