"""Outbound appointment SMS reminders via Twilio.

Sends a one-shot reminder ~24 hours before each appointment. No inbound
reply handling — confirmation is out of scope for this pass.

The FastAPI process runs ``run_reminder_tick`` on a background loop
(see ``server.py``). Each tick:

  1. Find appointments whose salon-local start is 23–25 hours from now
     and whose ``reminder_status`` is still ``pending`` or ``failed``.
  2. Send an SMS (or mark ``skipped`` if the phone is unusable).
  3. Update ``reminder_status`` / ``reminder_sent_at`` / ``reminder_error``.

Failed sends are retried on later ticks while still inside the window;
once the appointment is less than 23h away, we stop retrying.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from loguru import logger
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from db import session_scope
from models import Appointment, Salon as SalonRow
from salon import format_phone, normalize_phone

# Status values stored on Appointment.reminder_status.
STATUS_PENDING = "pending"
STATUS_SENT = "sent"
STATUS_FAILED = "failed"
STATUS_SKIPPED = "skipped"

# Send when the appointment start is this far in the future (±1h window
# so a missed poll or short outage still delivers).
REMINDER_LEAD = timedelta(hours=24)
REMINDER_WINDOW_HALF = timedelta(hours=1)  # → [23h, 25h]


def salon_tz() -> ZoneInfo:
    return ZoneInfo(os.getenv("SALON_TZ", "America/New_York"))


def to_e164(raw: str | None) -> str | None:
    """Normalize any US phone-ish string to ``+1XXXXXXXXXX`` for Twilio."""
    digits = normalize_phone(raw)
    if not digits:
        return None
    return f"+1{digits}"


def _appt_start_local(appt: Appointment) -> datetime:
    """Combine date + start_time as an aware datetime in the salon TZ."""
    naive = datetime.combine(appt.date, appt.start_time)
    return naive.replace(tzinfo=salon_tz())


def _first_name_for(appt: Appointment) -> str:
    if appt.client is not None and appt.client.first_name.strip():
        return appt.client.first_name.strip()
    parts = (appt.customer_name or "").strip().split(maxsplit=1)
    return parts[0] if parts else "there"


def _phone_for(appt: Appointment) -> str | None:
    """Prefer the linked client's canonical phone; fall back to the snapshot."""
    if appt.client is not None and appt.client.phone:
        return appt.client.phone
    return appt.customer_phone


def _location_short(sess) -> str:
    row = sess.scalar(select(SalonRow).order_by(SalonRow.id).limit(1))
    loc = (row.location if row else "") or "the salon"
    # Keep SMS short — first ~60 chars of the location string.
    loc = loc.strip()
    return loc if len(loc) <= 60 else loc[:57].rstrip() + "..."


def _fmt_time_12h(t) -> str:
    """``14:30`` → ``2:30 PM``."""
    h = t.hour % 12 or 12
    suffix = "AM" if t.hour < 12 else "PM"
    if t.minute:
        return f"{h}:{t.minute:02d} {suffix}"
    return f"{h} {suffix}"


def build_reminder_body(appt: Appointment, location: str) -> str:
    name = _first_name_for(appt)
    service = appt.service.name if appt.service is not None else "appointment"
    stylist = appt.staff.name if appt.staff is not None else "your stylist"
    when = _fmt_time_12h(appt.start_time)
    return (
        f"Hi {name}, reminder: you have a {service} with {stylist} "
        f"tomorrow at {when} at {location}. Reply STOP to opt out."
    )


def find_due_reminders(now_utc: datetime | None = None) -> list[str]:
    """Return appointment IDs that should receive a reminder on this tick.

    An appointment is due when its salon-local start falls in
    ``[now+23h, now+25h]`` and its reminder hasn't been successfully
    sent or permanently skipped yet.
    """
    now = now_utc or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    window_start = now + REMINDER_LEAD - REMINDER_WINDOW_HALF  # now+23h
    window_end = now + REMINDER_LEAD + REMINDER_WINDOW_HALF    # now+25h

    with session_scope() as sess:
        rows = sess.scalars(
            select(Appointment).where(
                Appointment.reminder_status.in_([STATUS_PENDING, STATUS_FAILED])
            )
        ).all()

        due_ids: list[str] = []
        for appt in rows:
            start = _appt_start_local(appt).astimezone(timezone.utc)
            if window_start <= start <= window_end:
                due_ids.append(appt.id)
        return due_ids


def _twilio_client():
    """Build a Twilio REST client, or raise with a clear message if misconfigured."""
    account_sid = os.getenv("TWILIO_ACCOUNT_SID", "").strip()
    auth_token = os.getenv("TWILIO_AUTH_TOKEN", "").strip()
    from_number = os.getenv("TWILIO_FROM_NUMBER", "").strip()
    if not account_sid or not auth_token:
        raise RuntimeError("TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN are not set.")
    if not from_number:
        raise RuntimeError(
            "TWILIO_FROM_NUMBER is not set. Use the salon voice number in E.164 "
            "(e.g. +12035551234)."
        )
    from twilio.rest import Client
    return Client(account_sid, auth_token), from_number


def send_appointment_reminder(appt_id: str) -> dict:
    """Send (or skip) the reminder for one appointment and persist the result.

    Returns a small status dict: ``{ok, status, error?}``.
    """
    with session_scope() as sess:
        appt = sess.scalar(
            select(Appointment)
            .where(Appointment.id == appt_id)
            .options(
                selectinload(Appointment.staff),
                selectinload(Appointment.service),
                selectinload(Appointment.client),
            )
        )
        if appt is None:
            return {"ok": False, "status": "missing", "error": f"Appointment {appt_id!r} not found."}

        if appt.reminder_status == STATUS_SENT:
            return {"ok": True, "status": STATUS_SENT}

        to_number = to_e164(_phone_for(appt))
        if not to_number:
            appt.reminder_status = STATUS_SKIPPED
            appt.reminder_error = "No usable US phone number on file."
            logger.warning(f"Reminder skipped for {appt_id}: unusable phone")
            return {"ok": False, "status": STATUS_SKIPPED, "error": appt.reminder_error}

        location = _location_short(sess)
        body = build_reminder_body(appt, location)

        try:
            client, from_number = _twilio_client()
            message = client.messages.create(
                to=to_number,
                from_=from_number,
                body=body,
            )
        except Exception as e:
            appt.reminder_status = STATUS_FAILED
            appt.reminder_error = str(e)[:500]
            logger.exception(f"Reminder failed for {appt_id} → {to_number}")
            return {"ok": False, "status": STATUS_FAILED, "error": appt.reminder_error}

        appt.reminder_status = STATUS_SENT
        appt.reminder_sent_at = datetime.now()
        appt.reminder_error = None
        logger.info(
            f"Reminder sent for {appt_id} → {format_phone(to_number[2:])} "
            f"(sid={message.sid})"
        )
        return {"ok": True, "status": STATUS_SENT, "sid": message.sid}


def run_reminder_tick() -> dict:
    """One poll cycle: find due appointments and try to send each reminder."""
    due_ids = find_due_reminders()
    summary = {"due": len(due_ids), "sent": 0, "failed": 0, "skipped": 0}
    if not due_ids:
        return summary

    logger.info(f"Reminder tick: {len(due_ids)} appointment(s) in the 24h window")
    for appt_id in due_ids:
        result = send_appointment_reminder(appt_id)
        status = result.get("status")
        if status == STATUS_SENT:
            summary["sent"] += 1
        elif status == STATUS_SKIPPED:
            summary["skipped"] += 1
        else:
            summary["failed"] += 1
    return summary
