# Placement Engine — one image for the API, the ingestion worker and the one-shot
# migration (Phase 1). Build it on the VM itself so it matches the VM's CPU
# (Ampere A1 = arm64, E2.1.Micro = amd64); nothing below is CPU-specific.
#
#   docker compose build                 # runtime image — what docker-compose.yml runs
#   docker build --target test .         # every verify_*.py suite on Python 3.12
#
# Only the files the processes need are copied. Secrets (.env), the Telegram
# session and databases never enter an image: .dockerignore keeps them out of the
# build context, and at run time they come from host volumes and env files.

# Multi-arch index digest of python:3.12.15-slim-trixie (amd64 + arm64).
# To update: pick a newer 3.12.x-slim tag, take its index digest
# (`docker buildx imagetools inspect python:<tag>`), rebuild and run the test stage.
ARG PYTHON_IMAGE=python:3.12.15-slim-trixie@sha256:a0f57064cbdcd6285fa45f51237ab0b35ea8ebaed06a1f5973e89e19dfb86704

# ── builder: dependencies into /opt/venv ──────────────────────────────────────
FROM ${PYTHON_IMAGE} AS builder
ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1
RUN python -m venv /opt/venv
COPY requirements-build.lock requirements.lock /tmp/
# Every package is hash-checked and installed from a wheel, except pyaes
# (Telethon's pure-Python AES fallback, published as source only). pyaes is built
# with the hash-pinned setuptools from requirements-build.lock rather than an
# unverified build backend, and setuptools is removed afterwards. A package with
# no wheel for this CPU fails the build instead of being compiled.
RUN /opt/venv/bin/pip install --require-hashes --no-deps -r /tmp/requirements-build.lock \
 && /opt/venv/bin/pip install --require-hashes --only-binary=:all: --no-binary=pyaes \
        --no-build-isolation -r /tmp/requirements.lock \
 && /opt/venv/bin/pip uninstall -y setuptools \
 && /opt/venv/bin/pip check

# ── app: code and a non-root user (shared by test and runtime) ────────────────
FROM ${PYTHON_IMAGE} AS app
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TMPDIR=/tmp
# Fixed IDs: the host directories mounted under /var/lib/placement are owned by
# 10001:10001 (deploy/RUNBOOK.md).
RUN groupadd --system --gid 10001 placement \
 && useradd --system --uid 10001 --gid placement --no-create-home \
        --home-dir /nonexistent --shell /usr/sbin/nologin placement \
 && install -d -o placement -g placement -m 0700 \
        /var/lib/placement/data /var/lib/placement/telegram
COPY --from=builder /opt/venv /opt/venv
WORKDIR /app
# Code stays owned by root: the app user can read it but not change it.
COPY alembic.ini ./
COPY alembic/ alembic/
COPY app/ app/
COPY scripts/login_telegram.py scripts/
RUN python -m compileall -q app alembic scripts
USER placement

# ── test: on demand only (`docker build --target test .`) ─────────────────────
# Runs as the app user against in-memory databases; nothing here reaches runtime.
FROM app AS test
COPY .dockerignore Dockerfile docker-compose.yml requirements.txt requirements.lock requirements-build.lock ./
COPY verify_*.py ./
COPY scripts/ scripts/
COPY deploy/ deploy/
RUN for suite in verify_*.py; do echo "== $suite"; python "$suite" || exit 1; done

# ── runtime: the default target; the worker and migrate override the command ──
FROM app AS runtime
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--no-server-header"]
