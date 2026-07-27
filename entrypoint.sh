#!/usr/bin/env sh
# Container entrypoint.
#
# Boot ordering:
#   1. Ensure the persistent volume's data directory exists.
#   2. Migrate the SQLite DB up to head (creates it on first boot).
#   3. If the DB is brand new AND a legacy salon workbook is present on
#      the volume (from before Phase 1), one-shot import it into the DB
#      and rename it to *.imported so we never re-run the import.
#   4. If the DB is still empty (fresh volume, no legacy xlsx), seed
#      from the image's baked-in workbook so the salon isn't literally
#      empty on first boot.
#   5. Exec the server.
#
# Everything from step 2 onward is idempotent on subsequent boots.

set -e

: "${SALON_DB_PATH:=/data/receptionist.db}"
: "${SALON_DATA_PATH:=/data/ReceptionistData.xlsx}"
SEED_XLSX="/app/ReceptionistData.xlsx.seed"

mkdir -p "$(dirname "$SALON_DB_PATH")"
mkdir -p "$(dirname "$SALON_DATA_PATH")"

export SALON_DB_PATH
export SALON_DATA_PATH

echo "Running alembic migrations against $SALON_DB_PATH"
python -m alembic upgrade head

# --- One-shot: legacy workbook on the volume -> DB ---
# On Fly volumes that were provisioned before Phase 1, /data holds the
# xlsx. If the DB has no data yet, migrate the workbook in and rename it
# so we don't re-import on the next boot. `import_xlsx --wipe` truncates
# first, so a partial import from a previous crashed boot is cleaned up.
DB_HAS_DATA=$(
    python - <<'PY'
from sqlalchemy import select
from db import session_scope
from models import Service, Staff, Appointment
with session_scope() as sess:
    for model in (Service, Staff, Appointment):
        if sess.scalar(select(model).limit(1)) is not None:
            print("yes"); break
    else:
        print("no")
PY
)

if [ "$DB_HAS_DATA" = "no" ]; then
    if [ -f "$SALON_DATA_PATH" ]; then
        echo "One-shot: importing legacy workbook $SALON_DATA_PATH into $SALON_DB_PATH"
        python -m import_xlsx "$SALON_DATA_PATH" --wipe
        mv "$SALON_DATA_PATH" "${SALON_DATA_PATH}.imported"
        echo "Legacy workbook renamed to ${SALON_DATA_PATH}.imported"
    elif [ -f "$SEED_XLSX" ]; then
        echo "Fresh volume: seeding DB from image seed workbook $SEED_XLSX"
        python -m import_xlsx "$SEED_XLSX" --wipe
    else
        echo "WARNING: no legacy workbook and no seed — starting with an empty salon." >&2
    fi
else
    echo "DB already has salon data; skipping xlsx import."
fi

exec python server.py
