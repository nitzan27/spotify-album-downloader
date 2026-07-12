# Build the React/TS SPA first. Discarded after `COPY --from=` below pulls
# only the built dist/ into the final image, so the runtime container stays
# Node-free - this stage exists purely to avoid committing frontend/dist to
# git (which would risk deploying a stale UI if someone forgets to rebuild).
FROM node:22-slim AS frontend-build

WORKDIR /frontend

COPY web/frontend/package.json web/frontend/package-lock.json ./
RUN npm ci

COPY web/frontend/ .
RUN npm run build

FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
COPY --from=frontend-build /frontend/dist ./web/frontend/dist

EXPOSE 8000

# Single worker: job state lives in in-process memory (see web/jobs.py) - a
# second worker process would never see jobs created on the first.
# Shell form (no brackets) so $PORT expands; Render assigns it at runtime.
CMD uvicorn web.app:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1
