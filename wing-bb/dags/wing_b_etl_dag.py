"""
wing_b_etl_dag.py — the Airflow DAG that schedules Wing B's pipeline.

WHERE THIS FILE LIVES:
NOT inside the wing-b application code. Airflow discovers DAGs by
scanning its own 'dags folder' (configured in airflow.cfg, or the
'dags.persistence' / git-sync settings of the Helm chart on k3s).
You copy or git-sync THIS file into that folder; Airflow parses it
and the DAG appears in the web UI automatically.

WHAT A DAG IS (study guide, section 6):
DAG = Directed Acyclic Graph. Nodes are tasks, edges are 'this must
finish before that starts'. Airflow runs the graph on a schedule,
retries failures with back-off, and shows green/red status per run.

STYLE NOTE:
This uses the modern 'TaskFlow API' — the @dag and @task decorators
(the same decorator concept from your Python study guide, section
5.1). Each decorated function becomes a node in the graph, and
CALLING one function with another's return value draws the edge.
"""

# datetime / timedelta: needed for the start date and retry delay.
from datetime import datetime, timedelta

# The two decorators that define the modern Airflow style.
from airflow.decorators import dag, task


# ── Default settings applied to EVERY task in this DAG ─────────────
default_args = {
    "owner": "wing-b",                       # shows in the UI, helps triage
    "retries": 3,                            # re-run a failed task up to 3x
    "retry_delay": timedelta(minutes=5),     # wait 5 min before retry #1
    "retry_exponential_backoff": True,       # then 10 min, then 20 min...
}


# ── The DAG definition ─────────────────────────────────────────────
@dag(
    dag_id="wing_b_sector_etl",              # unique name in the Airflow UI

    # Cron schedule: minute hour day month weekday.
    # "0 6 * * 1-5"  =  06:00 UTC, Monday–Friday only.
    # Why 6am: well before any market open, so the analyst dashboard
    # always shows fresh data when humans arrive.
    schedule="0 6 * * 1-5",

    # Airflow needs a fixed anchor date to compute schedule intervals
    # from. Any date in the past works; it does NOT backfill because
    # of catchup=False below.
    start_date=datetime(2026, 1, 1),

    # catchup=False: do NOT retroactively run every missed interval
    # between start_date and today. For a 'refresh latest data' job,
    # old runs would all do identical work — wasteful.
    catchup=False,

    default_args=default_args,

    # Tags are just labels for filtering DAGs in the UI.
    tags=["wing-b", "etl", "sector-data"],
)
def wing_b_sector_etl():
    """The pipeline: extract → transform → load → verify.

    We split the steps into separate TASKS (instead of one big task
    calling pipeline.run()) so that:
      - the UI shows exactly WHICH step failed,
      - a failed 'load' retries WITHOUT re-downloading the data,
      - on KubernetesExecutor each task runs as its own pod.
    """

    # Each @task function becomes one node in the graph. Airflow
    # serialises the return value and hands it to the next task —
    # so tasks exchange DATA by ordinary return/argument passing.
    # (Under the hood this is XCom; small data only. Big data should
    # be passed by writing to S3/DB and returning the PATH.)

    @task
    def extract() -> dict:
        """Fetch raw prices. Returns the frame as a dict so Airflow
        can serialise it between tasks. (Fine at our demo size; at
        real scale you'd write Parquet to S3 and return the path.)"""
        from etl.data_sources import fetch_prices   # import INSIDE the task:
        # the DAG file is parsed every ~30s by the scheduler, and
        # top-level heavy imports would slow every parse. Importing
        # inside the function defers the cost to actual runs.
        raw = fetch_prices()
        return raw.to_dict("list")                  # DataFrame → plain dict

    @task
    def transform(raw: dict) -> dict:
        """Clean and feature-engineer with the Polars pipeline."""
        import pandas as pd
        from etl.pipeline import clean_and_feature
        clean = clean_and_feature(pd.DataFrame(raw))
        return clean.to_pandas().to_dict("list")    # Polars → pandas → dict

    @task
    def load(clean: dict, raw: dict) -> int:
        """Write prices + fundamentals into the shared database.
        Returns the row count so the next task can sanity-check it."""
        import pandas as pd
        import polars as pl
        from etl.data_sources import fetch_fundamentals
        from etl.pipeline import load_to_db

        clean_pl = pl.from_pandas(pd.DataFrame(clean))
        funda = fetch_fundamentals(pd.DataFrame(raw))
        load_to_db(clean_pl, funda)
        return clean_pl.height                      # number of rows written

    @task
    def verify(row_count: int) -> None:
        """Fail loudly if the load looks wrong. A pipeline that
        'succeeds' while writing 0 rows is worse than one that fails —
        this guard turns silent corruption into a visible red task."""
        if row_count < 1000:                        # sane minimum for 2y x 16 tickers
            raise ValueError(
                f"Only {row_count} rows loaded — expected thousands. "
                "Upstream data source may be broken."
            )
        print(f"[verify] OK — {row_count:,} rows in sector_prices.")

    # ── Wire the graph ─────────────────────────────────────────────
    # Calling the functions in sequence DRAWS THE EDGES:
    #   extract → transform → load → verify
    raw = extract()
    clean = transform(raw)
    rows = load(clean, raw)
    verify(rows)


# Instantiate the DAG — this line is what Airflow's scanner finds.
wing_b_sector_etl()
