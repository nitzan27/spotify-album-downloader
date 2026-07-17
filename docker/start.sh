#!/bin/sh
set -e

# Backgrounded, not exec'd: if the PO-token sidecar dies, downloads should
# keep working (just without a token, same as before it existed), not take
# the whole app down. See the extractor_args note in album_downloader.py.
node /app/bgutil-server/build/main.js &

# Single worker: job state lives in in-process memory (see web/jobs.py) - a
# second worker process would never see jobs created on the first.
exec uvicorn web.app:app --host 0.0.0.0 --port "${PORT:-8000}" --workers 1
