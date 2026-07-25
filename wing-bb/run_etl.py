"""
run_etl.py — the front door for the ETL job.

Run it from the wing-b folder:
    python run_etl.py

In production this exact entry point is what an Airflow task or a
Kubernetes CronJob calls — locally and in prod, ONE code path.
"""

# This file lives at the project root, so plain imports of our
# packages (etl, common) work without any path tricks.
from etl.pipeline import run

# The classic Python guard: this block only executes when the file
# is RUN directly (python run_etl.py), not when it's IMPORTED by
# another module. It keeps 'import run_etl' side-effect free.
if __name__ == "__main__":
    run()
