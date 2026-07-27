"""One-shot: create Client rows from existing Appointment.customer_phone,
and link every Appointment to the resulting client.

Run this ONCE after deploying Phase 2. It's idempotent — running it twice
does nothing on the second pass because the FK is already set.

Usage:
    python -m backfill_clients            # dry run: report what would change
    python -m backfill_clients --commit   # actually write

Rationale: Phase 5's ``book_appointment`` auto-links new bookings, but the
appointments that already existed in production when Phase 2 shipped have
``client_id`` = NULL and won't be linked until this backfill runs. Once
executed the receptionist bot will greet returning callers even from
appointments booked before the Clients feature existed.
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from datetime import datetime

from loguru import logger
from sqlalchemy import select

from db import session_scope
from models import Appointment, Client as ClientRow
from salon import normalize_phone


def backfill(commit: bool) -> dict:
    """Return a summary dict of what happened (or would happen)."""
    created_clients = 0
    linked_appointments = 0
    unlinked_appointments = 0

    with session_scope() as sess:
        # Group appointments by normalized phone, ignoring rows already linked.
        by_phone: dict[str, list[Appointment]] = defaultdict(list)
        for appt in sess.scalars(select(Appointment).where(Appointment.client_id.is_(None))):
            normalized = normalize_phone(appt.customer_phone)
            if not normalized:
                unlinked_appointments += 1
                continue
            by_phone[normalized].append(appt)

        logger.info(
            f"Found {sum(len(v) for v in by_phone.values())} unlinked "
            f"appointment(s) across {len(by_phone)} unique phone number(s)."
        )
        if unlinked_appointments:
            logger.warning(
                f"{unlinked_appointments} appointment(s) have no usable phone "
                f"number and cannot be linked."
            )

        now = datetime.now()
        for phone, appts in by_phone.items():
            # Pick the most-recent non-empty name from these appointments
            # (the LLM's latest capture is usually cleaner than the earliest).
            appts_by_recency = sorted(appts, key=lambda a: a.created_at, reverse=True)
            best_name = ""
            for a in appts_by_recency:
                if a.customer_name.strip():
                    best_name = a.customer_name.strip()
                    break

            first, last = "", ""
            if best_name:
                parts = best_name.split(maxsplit=1)
                first = parts[0]
                last = parts[1] if len(parts) > 1 else ""

            client_row = sess.scalar(select(ClientRow).where(ClientRow.phone == phone))
            if client_row is None:
                created_clients += 1
                logger.info(
                    f"  New client: {best_name or '(no name)'} @ {phone} "
                    f"(would link {len(appts)} appointment(s))"
                )
                if commit:
                    client_row = ClientRow(
                        first_name=first,
                        last_name=last,
                        phone=phone,
                        created_at=now,
                        updated_at=now,
                    )
                    sess.add(client_row)
                    sess.flush()
            else:
                logger.info(
                    f"  Existing client id={client_row.id}: link {len(appts)} appointment(s)"
                )

            for a in appts:
                if commit and client_row is not None:
                    a.client_id = client_row.id
                linked_appointments += 1

        if not commit:
            # Roll back so a dry run leaves the DB untouched. session_scope
            # would otherwise commit at the end of the `with` block.
            sess.rollback()

    return {
        "created_clients": created_clients,
        "linked_appointments": linked_appointments,
        "unlinked_appointments": unlinked_appointments,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--commit",
        action="store_true",
        help="Actually write. Without this flag the script only reports.",
    )
    args = parser.parse_args(argv)

    result = backfill(commit=args.commit)
    verb = "Created" if args.commit else "Would create"
    verb_link = "Linked" if args.commit else "Would link"
    print()
    print(f"{verb} {result['created_clients']} new client row(s).")
    print(f"{verb_link} {result['linked_appointments']} appointment(s) to a client.")
    if result["unlinked_appointments"]:
        print(
            f"{result['unlinked_appointments']} appointment(s) still unlinked "
            f"(missing/unparseable phone)."
        )
    if not args.commit:
        print("\nDry run — no changes written. Re-run with --commit to apply.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
