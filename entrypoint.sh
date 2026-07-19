#!/bin/sh
# Container start-up: bring the schema up to date, then hand off to gunicorn.
# `exec` replaces this shell with gunicorn so signals (SIGTERM on
# `docker stop` / k8s pod shutdown) reach the server directly.
set -e

echo "[entrypoint] Applying database migrations (flask db upgrade)..."
FLASK_APP=manage.py flask db upgrade

echo "[entrypoint] Starting gunicorn on :5000 ..."
# gthread workers + 1 process: ContextFlow's rate limiter is in-memory and
# per-process, so a single process keeps limits accurate; threads give the
# concurrency (the app is I/O-bound on OpenAI calls, and SSE streams need a
# free thread each). Scaling past one worker means moving limits to Redis
# first (see the Phase 7 hardening notes).
exec gunicorn \
    --worker-class gthread \
    --workers "${WEB_WORKERS:-1}" \
    --threads "${WEB_THREADS:-8}" \
    --timeout 120 \
    --bind 0.0.0.0:5000 \
    --access-logfile - \
    wsgi:app
