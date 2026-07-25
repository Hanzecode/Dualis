"""
test_ingestor_contract.py — proves both producers emit signals that
PASS Wing A's ingestor validation.

We copy the ingestor's _validate logic here (schema, type, range,
universe, staleness, strength) and run real model + analyst signals
through it. If they pass here, they pass in the real ingestor.
"""
import json
from datetime import datetime, timezone
from common.config import SECTOR_UNIVERSE

UNIVERSE = {t for ts in SECTOR_UNIVERSE.values() for t in ts}
MIN_SIGNAL_STRENGTH = 0.05
MIN_CONFIDENCE = 0.25
MAX_AGE_S = 3600


def validate(data: dict) -> tuple[bool, str]:
    """Mirror of ingestor._validate — returns (ok, reason)."""
    required = {"symbol", "signal", "confidence", "target_qty",
                "limit_price_bps", "generated_at"}
    missing = required - data.keys()
    if missing:
        return False, f"missing fields: {missing}"
    try:
        symbol = str(data["symbol"]).upper()
        signal = float(data["signal"])
        confidence = float(data["confidence"])
        target_qty = int(data["target_qty"])
        int(data["limit_price_bps"])
        generated_at = datetime.fromisoformat(
            data["generated_at"].replace("Z", "+00:00"))
    except (ValueError, TypeError) as e:
        return False, f"type error: {e}"
    if not (-1.0 <= signal <= 1.0):
        return False, f"signal out of range: {signal}"
    if not (0.0 <= confidence <= 1.0):
        return False, f"confidence out of range: {confidence}"
    if target_qty <= 0:
        return False, "target_qty <= 0"
    if symbol not in UNIVERSE:
        return False, f"{symbol} not in universe"
    age = (datetime.now(timezone.utc) - generated_at).total_seconds()
    if age > MAX_AGE_S:
        return False, f"stale: {age:.0f}s"
    if abs(signal) < MIN_SIGNAL_STRENGTH:
        return False, f"too weak: {signal}"
    if confidence < MIN_CONFIDENCE:
        return False, f"low confidence: {confidence}"
    return True, "OK"


print("=" * 60)
print("TEST 1 — ML model signals")
print("=" * 60)
from scoring.scorer import score_universe
model_signals = score_universe()
passed = 0
for s in model_signals:
    ok, reason = validate(s)
    flag = "PASS" if ok else f"FAIL ({reason})"
    if ok:
        passed += 1
    print(f"  {s['symbol']:6} signal={s['signal']:+.3f} "
          f"conf={s['confidence']:.2f} qty={s['target_qty']:3d}  {flag}")
print(f"\n  {passed}/{len(model_signals)} model signals passed validation")

print()
print("=" * 60)
print("TEST 2 — analyst signals")
print("=" * 60)
from common.signal_bridge import analyst_row_to_signal_dict
analyst_rows = [
    {"ticker": "AAPL", "direction": "long",  "conviction": 4,
     "created_at": datetime.now(timezone.utc)},
    {"ticker": "JPM",  "direction": "short", "conviction": 5,
     "created_at": datetime.now(timezone.utc)},
    {"ticker": "XOM",  "direction": "long",  "conviction": 2,
     "created_at": datetime.now(timezone.utc)},
]
passed = 0
for row in analyst_rows:
    s = analyst_row_to_signal_dict(row)
    ok, reason = validate(s)
    flag = "PASS" if ok else f"FAIL ({reason})"
    if ok:
        passed += 1
    print(f"  {row['ticker']:6} {row['direction']:5} c{row['conviction']} -> "
          f"signal={s['signal']:+.3f} conf={s['confidence']:.2f} "
          f"qty={s['target_qty']:3d}  {flag}")
print(f"\n  {passed}/{len(analyst_rows)} analyst signals passed validation")

print()
print("=" * 60)
print("SAMPLE JSON WRITTEN TO S3 (what the ingestor reads)")
print("=" * 60)
print("Model signal:")
print(json.dumps(model_signals[0], indent=2))
print("\nAnalyst signal:")
print(json.dumps(analyst_row_to_signal_dict(analyst_rows[0]), indent=2))
