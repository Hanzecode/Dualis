#!/bin/sh
# wing-bb/docker/entrypoint.sh — mirrors exactly how this project has been
# run by hand all along: one ETL pass, then score on a loop so Wing A always
# has fresh signals to poll. Not a fixed system parameter — SCORE_INTERVAL_S
# just controls how often *this container* re-scores; see WING_B_DEEP_DIVE.md
# for why 30s doesn't mean the underlying fundamentals actually change that
# fast.
set -e

echo "[entrypoint] running ETL..."
python run_etl.py

INTERVAL="${SCORE_INTERVAL_S:-30}"
echo "[entrypoint] scoring every ${INTERVAL}s..."
while true; do
    python run_score.py
    sleep "$INTERVAL"
done
