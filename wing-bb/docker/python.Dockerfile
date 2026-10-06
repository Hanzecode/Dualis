# wing-bb/docker/python.Dockerfile — Wing B's ETL + scoring loop.
#
# Wing B has no single long-running server process — run_etl.py and
# run_score.py are one-shot scripts. entrypoint.sh reproduces exactly how
# this project has actually been run by hand: ETL once, then score on a
# loop so signals stay fresh.
#
# Build from the wing-bb/ directory as context:
#   docker build -f docker/python.Dockerfile -t dualis-wing-b .

FROM python:3.11-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY requirements.txt .
# apache-airflow is a heavy, unused dependency here (see README — the DAGs
# exist but nothing in this project's actual run path uses Airflow). Skipping
# it keeps the image a fraction of the size without touching the shared
# requirements.txt that documents everything the project as a whole can do.
RUN grep -v '^apache-airflow' requirements.txt > requirements.docker.txt \
    && pip install --no-cache-dir -r requirements.docker.txt

COPY . .
RUN chmod +x docker/entrypoint.sh

ENTRYPOINT ["docker/entrypoint.sh"]
