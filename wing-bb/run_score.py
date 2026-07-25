"""
run_score.py — front door for the ML scoring job.

    python run_score.py

Runs the valuation model, converts each stock's upside into a
normalised alpha signal, and writes them to S3 in Wing A's ingestor
contract. In production an Airflow task / Kubernetes CronJob calls this.
"""

from scoring.scorer import run

if __name__ == "__main__":
    run()
