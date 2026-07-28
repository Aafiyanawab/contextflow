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
# Upgrade the venv's build tools to patched versions BEFORE installing deps.
# setuptools is pinned to the 81.x line on purpose: it vendors patched copies of
# wheel (0.46.3, CVE-2026-24049) and jaraco.context (6.1.0, CVE-2026-23949) in
# setuptools/_vendor, which Trivy flags in the base image's 79.x. setuptools 82+
# drops pkg_resources, which some runtime deps still import — so unpinned
# "latest" (83) both fixes the CVEs and risks a runtime ImportError. 81.x is the
# only line that patches the vendored CVEs while keeping pkg_resources.
RUN pip install --no-cache-dir --upgrade pip "setuptools>=81.0.0,<82" wheel \
    && pip install -r requirements.txt

# ---------- Stage 2: runtime — slim, non-root, venv copied in ----------
FROM python:3.11-slim
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH"
# Patch base-OS packages so the image picks up fixed Debian security updates —
# the Trivy image gate fails on FIXABLE HIGH/CRITICAL OS CVEs, and a rolling
# base tag can lag the latest fixes. apt lists are removed to keep the layer small.
RUN apt-get update && apt-get upgrade -y \
    && rm -rf /var/lib/apt/lists/*
# The slim base ships setuptools 79.x in the system site-packages, whose
# setuptools/_vendor bundles the vulnerable wheel 0.45.1 and jaraco.context
# 5.3.0. The app runs from /opt/venv, but Trivy scans the whole image — so patch
# the base interpreter's setuptools to the same 81.x line (vendors wheel 0.46.3 +
# jaraco.context 6.1.0). No-cache install; drop pip's cache to keep the layer small.
RUN pip install --no-cache-dir --upgrade "setuptools>=81.0.0,<82" \
    && rm -rf /root/.cache/pip
# Never run as root in a container: create an unprivileged user.
RUN adduser --disabled-password --gecos "" appuser
WORKDIR /app
COPY --from=builder /opt/venv /opt/venv
COPY . .
RUN chmod +x entrypoint.sh && chown -R appuser:appuser /app
USER appuser
EXPOSE 5000
ENTRYPOINT ["./entrypoint.sh"]
