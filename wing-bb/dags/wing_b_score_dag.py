"""
dags/wing_b_score_dag.py — schedules the ML scorer.

This is the 'score DAG' Wing A's ingestor.py refers to in its comment.
It runs the valuation model and writes normalised alpha signals to S3
in the ingestor contract. Wing A's ingestor polls that S3 prefix and
picks them up — no direct coupling between the DAGs.

Runs more often than the ETL because signals are the fast-moving output.

SCHEDULE NOTE: the "real" production schedule is every 15 minutes during
market hours — "*/15 9-16 * * 1-5" (9:00-16:00 UTC, Mon-Fri), see below.
For local/paper-trading dev we instead run every 2 minutes with no
day/hour restriction, so Wing A keeps getting fresh signals continuously
regardless of what time it is on your machine — also comfortably inside
Wing A's 300s (5 min) max_signal_age_s staleness window. Swap back to the
market-hours cron before pointing this at a real trading day.
"""

from datetime import datetime, timedelta
from airflow.decorators import dag, task

default_args = {
    "owner": "wing-b",
    "retries": 2,
    "retry_delay": timedelta(minutes=2),
}


@dag(
    dag_id="wing_b_score",
    # Production schedule (market hours only): "*/15 9-16 * * 1-5"
    # Local/paper-trading dev schedule — every 2 minutes, always on:
    schedule="*/2 * * * *",
    start_date=datetime(2026, 1, 1),
    catchup=False,
    default_args=default_args,
    tags=["wing-b", "scoring", "signals"],
)
def wing_b_score():

    @task
    def score_and_write() -> int:
        """Run the valuation model, convert to signals, write to S3.
        Returns the count so a downstream check can verify it."""
        from scoring.scorer import score_universe
        from common.signal_writer import write_signals

        signals = score_universe()
        paths = write_signals(signals, source="model")
        return len(paths)

    @task
    def verify(count: int) -> None:
        """Fail loudly if the scorer wrote nothing — a silent empty
        run would starve Wing A of signals."""
        if count < 1:
            raise ValueError("Scorer wrote 0 signals — model or data broken.")
        print(f"[score] OK — {count} signals written to S3.")

    verify(score_and_write())


wing_b_score()