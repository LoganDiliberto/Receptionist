# syntax=docker/dockerfile:1.7
#
# Two stages so the final image doesn't carry Node or the Angular build
# toolchain. Stage 1 builds the admin UI, stage 2 runs the FastAPI server
# and copies the built assets over.

# ---------- Stage 1: build the Angular admin UI ----------
FROM node:22-alpine AS admin-ui-build
WORKDIR /admin-ui

# Install deps in a layer that only rebuilds when the manifest changes.
COPY admin-ui/package.json admin-ui/package-lock.json ./
RUN npm ci

COPY admin-ui/ ./
RUN npm run build

# ---------- Stage 2: Python runtime ----------
FROM python:3.11-slim AS runtime

# Piper needs libgomp1 at runtime for its ONNX runtime backend; ffmpeg gives
# us broad audio format compatibility. Everything else is pure Python or
# ships wheels.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        libgomp1 \
        ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps first so subsequent code changes don't invalidate this
# layer (biggest image build cost by far).
COPY requirements.txt ./
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

# Pre-download the Piper voice into /app/voices at build time so the very
# first call doesn't have to wait ~63 MB of download.
# PIPER_VOICE can be overridden here to bake a different voice into the image.
ARG PIPER_VOICE=en_US-amy-medium
RUN mkdir -p /app/voices \
    && python -c "from pathlib import Path; from piper.download_voices import download_voice; \
       download_voice('${PIPER_VOICE}', Path('/app/voices'))"

# App source (order matters for cache: most-frequently-changed files last).
COPY salon.py db.py models.py admin_api.py calls.py bot.py server.py entrypoint.sh ./
COPY import_xlsx.py export_xlsx.py backfill_clients.py reminders.py ./
COPY alembic.ini ./
COPY alembic/ ./alembic/
COPY static/ ./static/
# Legacy seed workbook, only used by entrypoint.sh on very first boot of a
# fresh volume when neither the DB nor a legacy /data/ReceptionistData.xlsx
# exist. Once the DB is populated the seed is never touched again.
COPY ReceptionistData.xlsx ./ReceptionistData.xlsx.seed

# Built Angular assets — the FastAPI server serves them from this exact path.
COPY --from=admin-ui-build /admin-ui/dist/admin-ui/browser/ ./admin-ui/dist/admin-ui/browser/

RUN chmod +x /app/entrypoint.sh \
    && mkdir -p /data /app/logs

# Fly.io defaults to $PORT=8080, but our server also honors $PORT — no hardcoded value.
ENV HOST=0.0.0.0 \
    PORT=8080 \
    SALON_DB_PATH=/data/receptionist.db \
    SALON_DATA_PATH=/data/ReceptionistData.xlsx \
    PYTHONUNBUFFERED=1

EXPOSE 8080

ENTRYPOINT ["/app/entrypoint.sh"]
