# syntax=docker/dockerfile:1

# ---------- Stage 1: builder — install deps into an isolated venv ----------
# A separate build stage keeps pip, its cache, and any build tooling out of
# the final image. We copy just the venv forward, so the runtime layer stays
# small and has no build-time cruft.
FROM python:3.11-slim AS builder
ENV PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
# Copy requirements first so this layer is cached and only re-runs when
# dependencies change — not on every source edit.
COPY requirements.txt .
RUN pip install -r requirements.txt

# ---------- Stage 2: runtime — slim, non-root, venv copied in ----------
FROM python:3.11-slim
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH"
# Never run as root in a container: create an unprivileged user.
RUN adduser --disabled-password --gecos "" appuser
WORKDIR /app
COPY --from=builder /opt/venv /opt/venv
COPY . .
RUN chmod +x entrypoint.sh && chown -R appuser:appuser /app
USER appuser
EXPOSE 5000
ENTRYPOINT ["./entrypoint.sh"]
