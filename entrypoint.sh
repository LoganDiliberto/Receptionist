#!/usr/bin/env sh
# Container entrypoint.
#
# The salon xlsx workbook needs to live on the persistent volume at
# $SALON_DATA_PATH (default /data/ReceptionistData.xlsx). On the very first
# boot of a new volume, that path doesn't exist yet, so we seed it from the
# baked-in copy that ships in the image. On every subsequent boot the
# volume's copy is used untouched (and any admin-UI edits or bot bookings
# persist across redeploys).

set -e

: "${SALON_DATA_PATH:=/data/ReceptionistData.xlsx}"
SEED_XLSX="/app/ReceptionistData.xlsx.seed"

mkdir -p "$(dirname "$SALON_DATA_PATH")"

if [ ! -f "$SALON_DATA_PATH" ]; then
    if [ -f "$SEED_XLSX" ]; then
        echo "Seeding $SALON_DATA_PATH from image seed ($SEED_XLSX)"
        cp "$SEED_XLSX" "$SALON_DATA_PATH"
    else
        echo "WARNING: no seed workbook at $SEED_XLSX; salon.py will fail to load." >&2
    fi
else
    echo "Using existing salon workbook at $SALON_DATA_PATH"
fi

export SALON_DATA_PATH

exec python server.py
