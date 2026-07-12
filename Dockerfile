FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000

# Single worker: job state lives in in-process memory (see web/jobs.py) - a
# second worker process would never see jobs created on the first.
# Shell form (no brackets) so $PORT expands; Render assigns it at runtime.
CMD uvicorn web.app:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1
