# wing-a/docker/python.Dockerfile — Wing A's Python process (main.py).
#
# This single process covers order sizing, both Python-side risk checks,
# TWAP slicing, the Alpaca quote feed, AND the FastAPI dashboard API —
# main.py starts uvicorn on a background thread itself, so there's nothing
# extra to run for the dashboard backend.
#
# Build from the wing-a/ directory as context:
#   docker build -f docker/python.Dockerfile -t dualis-wing-a .

FROM python:3.11-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1

# libpq5: runtime lib for psycopg2-binary's Postgres protocol support.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libpq5 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# main.py's logging setup assumes logs/ already exists (true on the host
# from earlier runs, not true in a fresh container filesystem).
RUN mkdir -p logs

# 8000 = the FastAPI dashboard API main.py starts internally.
EXPOSE 8000

CMD ["python", "main.py"]
