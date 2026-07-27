"""Salon data store: hours, staff, services, schedules, and appointments.

Backed by SQLite via SQLAlchemy. Static info (staff list, services,
schedules, hours, location) is cached in ``INFO`` on startup and
refreshed via ``reload()`` after any admin mutation; the voice bot reads
from that cache to avoid a DB round-trip on every LLM tool call.

Mutations always go through the public helpers below, which use a
transaction and then call ``reload()`` — the next call sees the new
state.

Public surface used by the voice bot (`bot.py`):
  - INFO                       (cached SalonInfo)
  - SERVICE_DURATIONS_MIN      (derived {name.lower(): minutes})
  - system_prompt_context()    (rendered block for the system prompt)
  - check_availability(...)    (LLM tool)
  - book_appointment(...)      (LLM tool)

Public surface used by the admin API (`admin_api.py`):
  - list/create/update/delete for staff, services, appointments
  - get/update for hours and location
  - reload() to refresh in-memory state after a mutation
"""

from __future__ import annotations

import asyncio
import re
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from threading import RLock
from typing import Iterable

from loguru import logger
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from db import session_scope
from models import (
    Appointment,
    Client as ClientRow,
    Hours as HoursRow,
    Salon as SalonRow,
    Service as ServiceRow,
    Staff as StaffRow,
    StaffHours as StaffHoursRow,
)

# The canonical order we render weekdays in everywhere (schedules, hours, UI).
WEEKDAYS = ["Sunday", "Monday", "Tuesday", "Wednesday",
            "Thursday", "Friday", "Saturday"]

SLOT_MIN = 30  # all appointments align to 30-minute slot boundaries

# Reentrant lock guarding the cached ``INFO`` and serializing writes.
# SQLite itself serializes writes at the file level, but the salon-side
# logic (validate -> check conflicts -> insert) needs to be atomic
# across those steps too. A single async lock ensures the two bot tools
# don't race with each other or with admin edits.
_lock = RLock()
_async_lock = asyncio.Lock()


# ---------- Parsing helpers ----------


def _parse_time(s: str) -> time:
    """Parse '10AM', '12 PM', '7PM', '10:00 AM' -> datetime.time."""
    s = s.strip().upper().replace(" ", "")
    m = re.match(r"(\d{1,2})(?::(\d{2}))?(AM|PM)", s)
    if not m:
        raise ValueError(f"Cannot parse time: {s!r}")
    hour = int(m.group(1))
    minute = int(m.group(2) or 0)
    if m.group(3) == "PM" and hour < 12:
        hour += 12
    if m.group(3) == "AM" and hour == 12:
        hour = 0
    return time(hour, minute)


def _parse_date(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


def _parse_clock(s: str) -> time:
    """Parse '14:30' or '2:30 PM' -> datetime.time."""
    s = s.strip().upper()
    if re.search(r"AM|PM", s):
        return _parse_time(s)
    h, m = s.split(":")
    return time(int(h), int(m))


def _weekday_name(d: date) -> str:
    # Python's Monday=0, Sunday=6.
    return ["Monday", "Tuesday", "Wednesday", "Thursday",
            "Friday", "Saturday", "Sunday"][d.weekday()]


def normalize_phone(raw: str | None) -> str:
    """Reduce any US phone number to its canonical storage form.

    Strips every non-digit character and keeps only the last 10 digits so
    the following all compare equal in the DB:

        +1 (203) 359-4344   ->  2033594344
        1-203-359-4344      ->  2033594344
        203.359.4344        ->  2033594344
        2033594344          ->  2033594344

    Returns ``""`` for anything with fewer than 10 digits (empty string,
    obviously-wrong input, etc). Callers that need to distinguish "no
    phone" from "bad phone" should check the raw input themselves before
    calling this.
    """
    if not raw:
        return ""
    digits = re.sub(r"\D", "", raw)
    return digits[-10:] if len(digits) >= 10 else ""


def format_phone(normalized: str | None) -> str:
    """Render a normalized 10-digit phone the way a person would say it.

    ``"2033594344"`` -> ``"(203) 359-4344"``. Falls back to the input as-is
    for anything that isn't a clean 10-digit number so we never crash a
    system-prompt render on unexpected data.
    """
    if not normalized:
        return ""
    digits = re.sub(r"\D", "", normalized)
    if len(digits) != 10:
        return normalized
    return f"({digits[:3]}) {digits[3:6]}-{digits[6:]}"


def _fmt_range(start: time, end: time) -> str:
    """Render a schedule range in the '10AM to 4PM' style the sheet uses.

    Kept for backwards compat with import/export CLIs and the existing
    ``system_prompt_context`` renderer.
    """

    def one(t: time) -> str:
        suffix = "AM" if t.hour < 12 else "PM"
        h = t.hour % 12 or 12
        return f"{h}:{t.minute:02d}{suffix}" if t.minute else f"{h}{suffix}"

    return f"{one(start)} to {one(end)}"


# ---------- Data model (cached in-memory) ----------


@dataclass(frozen=True)
class Service:
    name: str
    duration_minutes: int
    price: float


@dataclass(frozen=True)
class Stylist:
    name: str
    services: tuple[str, ...]  # lowercased service names
    schedule: dict[str, tuple[time, time]]  # weekday -> (start, end)


@dataclass
class SalonInfo:
    location: str
    hours: dict[str, tuple[time, time] | None]  # weekday -> (open, close) or None
    stylists: dict[str, Stylist]
    services: dict[str, Service] = field(default_factory=dict)


# ---------- Read-side: load a fresh SalonInfo from the DB ----------


def _load_info() -> SalonInfo:
    with session_scope() as sess:
        # Location — single-row table. Missing row is fine on a freshly
        # migrated DB; caller can populate via update_location.
        salon_row = sess.scalar(select(SalonRow).order_by(SalonRow.id).limit(1))
        location = salon_row.location if salon_row else ""

        # Hours — one row per weekday. Fill in any missing weekdays as
        # "unknown, treat as closed" so callers can rely on all seven
        # keys being present.
        hours: dict[str, tuple[time, time] | None] = {d: None for d in WEEKDAYS}
        for h in sess.scalars(select(HoursRow)):
            if h.open is None or h.close is None:
                hours[h.weekday] = None
            else:
                hours[h.weekday] = (h.open, h.close)

        # Services
        services: dict[str, Service] = {}
        for row in sess.scalars(select(ServiceRow).order_by(ServiceRow.name)):
            services[row.name.lower()] = Service(
                name=row.name,
                duration_minutes=row.duration_minutes,
                price=row.price,
            )

        # Staff, with their offered services and weekly schedule.
        stylists: dict[str, Stylist] = {}
        stmt = (
            select(StaffRow)
            .options(selectinload(StaffRow.services), selectinload(StaffRow.hours))
            .order_by(StaffRow.name)
        )
        for s in sess.scalars(stmt):
            schedule = {
                sh.weekday: (sh.start, sh.end) for sh in s.hours
            }
            offered = tuple(sorted(svc.name.lower() for svc in s.services))
            stylists[s.name] = Stylist(
                name=s.name, services=offered, schedule=schedule
            )

    return SalonInfo(
        location=location, hours=hours, stylists=stylists, services=services
    )


# Module-level cache. Populated on import; refreshed via ``reload()``.
INFO: SalonInfo = _load_info()
SERVICE_DURATIONS_MIN: dict[str, int] = {
    key: svc.duration_minutes for key, svc in INFO.services.items()
}
logger.info(
    f"Salon loaded from DB: "
    f"{len(INFO.stylists)} staff ({', '.join(INFO.stylists) or 'none'}), "
    f"{len(INFO.services)} services"
)


def reload() -> None:
    """Refresh cached state after a DB mutation. Cheap; hits ~5 tables."""
    global INFO, SERVICE_DURATIONS_MIN
    with _lock:
        INFO = _load_info()
        SERVICE_DURATIONS_MIN = {
            key: svc.duration_minutes for key, svc in INFO.services.items()
        }


# ---------- Read-side helpers (DB row -> wire dict) ----------


def _appt_to_dict(a: Appointment) -> dict:
    """Serialize an Appointment row to a JSON-friendly dict.

    The old xlsx-era shape is preserved (existing UI keys unchanged),
    with two Phase-2 additions: ``client_id`` and (when the appointment
    is linked to a client) ``client``, a small nested object the admin
    UI can render as a "known caller" badge.
    """
    return {
        "id": a.id,
        "created_at": a.created_at.isoformat(timespec="seconds"),
        "customer_name": a.customer_name,
        "customer_phone": a.customer_phone,
        "stylist": a.staff.name,
        "service": a.service.name,
        "date": a.date.isoformat(),
        "start_time": a.start_time.strftime("%H:%M"),
        "end_time": a.end_time.strftime("%H:%M"),
        "session_id": a.session_id,
        "client_id": a.client_id,
        "client": (
            {
                "id": a.client.id,
                "first_name": a.client.first_name,
                "last_name": a.client.last_name,
                "phone": a.client.phone,
                "phone_formatted": format_phone(a.client.phone),
            }
            if a.client is not None
            else None
        ),
    }


# ---------- Public salon-context helper for the system prompt ----------


def _fmt_hour(t: time) -> str:
    """Cross-platform hour-only 12-hour format ('7 PM')."""
    suffix = "AM" if t.hour < 12 else "PM"
    h = t.hour % 12 or 12
    return f"{h} {suffix}"


def system_prompt_context() -> str:
    """Render salon static info as a block to inject into the LLM system prompt."""
    lines = ["SALON LOCATION:", f"  {INFO.location}", "", "SALON HOURS:"]
    for day in WEEKDAYS[1:] + [WEEKDAYS[0]]:  # Monday..Sunday
        h = INFO.hours.get(day)
        if h is None:
            lines.append(f"  {day}: closed")
        else:
            lines.append(f"  {day}: {_fmt_hour(h[0])} to {_fmt_hour(h[1])}")

    lines += ["", "STAFF AND SERVICES:"]
    for name, s in INFO.stylists.items():
        services = ", ".join(sorted(s.services))
        lines.append(f"  {name}: {services}")

    lines += ["", "STAFF SCHEDULES:"]
    for name, s in INFO.stylists.items():
        days = []
        for day in WEEKDAYS[1:] + [WEEKDAYS[0]]:
            if day in s.schedule:
                start, end = s.schedule[day]
                days.append(f"{day[:3]} {_fmt_range(start, end)}")
        lines.append(f"  {name}: {', '.join(days) if days else 'not scheduled'}")

    lines += ["", "SERVICE DURATIONS (minutes):"]
    for svc in INFO.services.values():
        lines.append(f"  {svc.name}: {svc.duration_minutes}")

    return "\n".join(lines)


def caller_context(caller_phone: str | None) -> str:
    """Render a per-call "who's on the line" block for the system prompt.

    Returns a string that either identifies a returning caller (with a
    short appointment history) or flags them as new. Always safe to
    inject verbatim — never raises. When ``caller_phone`` is ``None``
    (e.g. the browser test transport) the block explains that so the
    LLM doesn't try to reference a caller number that doesn't exist.
    """
    if not caller_phone:
        return (
            "CALLER INFO:\n"
            "  Phone number: unknown (browser test session, no caller ID).\n"
            "  On file: N/A."
        )

    normalized = normalize_phone(caller_phone)
    if not normalized:
        return (
            f"CALLER INFO:\n"
            f"  Phone number: {caller_phone} (couldn't parse — treat as unknown).\n"
            f"  On file: No."
        )

    formatted = format_phone(normalized)
    client = get_client_by_phone(normalized)
    if client is None:
        return (
            f"CALLER INFO:\n"
            f"  Phone number: {formatted}\n"
            f"  On file: No — this is a new caller. Collect their full "
            f"name during booking, but DO NOT ask for a callback phone "
            f"number — we already have it above."
        )

    full_name = f"{client['first_name']} {client['last_name']}".strip()
    lines = [
        "CALLER INFO:",
        f"  Phone number: {formatted}",
        "  On file: Yes",
        f"  Name: {full_name or '(no name on file)'}",
    ]
    if client.get("gender"):
        lines.append(f"  Gender: {client['gender']}")
    if client.get("notes"):
        # Truncate long notes so a stylist's essay doesn't dominate the
        # system prompt and blow the LLM's attention budget.
        notes = str(client["notes"]).strip()
        if len(notes) > 300:
            notes = notes[:297] + "..."
        lines.append(f"  Notes: {notes}")

    # Upcoming (client_history with upcoming_only=True) + last 3 past for context.
    upcoming = client_history(client["id"], upcoming_only=True)
    all_history = client_history(client["id"])
    past = [a for a in all_history if a["date"] < date.today().isoformat()][:3]

    if upcoming:
        lines.append("  Upcoming appointments:")
        for a in upcoming:
            lines.append(
                f"    - {a['service']} with {a['stylist']} on "
                f"{a['date']} at {a['start_time']}"
            )
    else:
        lines.append("  Upcoming appointments: none")

    if past:
        lines.append("  Recent past appointments (last 3):")
        for a in past:
            lines.append(
                f"    - {a['service']} with {a['stylist']} on {a['date']}"
            )
    return "\n".join(lines)


# ---------- Slot math ----------


def _slots_in_range(start: time, end: time) -> list[time]:
    out = []
    cur = datetime.combine(date.today(), start)
    last = datetime.combine(date.today(), end)
    while cur + timedelta(minutes=SLOT_MIN) <= last:
        out.append(cur.time())
        cur += timedelta(minutes=SLOT_MIN)
    return out


def _overlaps(a_start: time, a_end: time, b_start: time, b_end: time) -> bool:
    return not (a_end <= b_start or a_start >= b_end)


# ---------- Tool: check_availability ----------


async def check_availability(
    date_iso: str,
    stylist: str | None = None,
    service: str | None = None,
) -> dict:
    """Return open appointment slots on the given date, optionally filtered."""
    async with _async_lock:
        return await asyncio.to_thread(_check_availability_sync, date_iso, stylist, service)


def _check_availability_sync(date_iso: str, stylist: str | None, service: str | None) -> dict:
    try:
        d = _parse_date(date_iso)
    except ValueError:
        return {"error": f"Invalid date {date_iso!r}. Use YYYY-MM-DD."}

    weekday = _weekday_name(d)

    if INFO.hours.get(weekday) is None:
        return {"date": date_iso, "weekday": weekday, "closed": True, "available": []}
    salon_open, salon_close = INFO.hours[weekday]

    candidates = list(INFO.stylists.values())
    if stylist:
        candidates = [s for s in candidates if s.name.lower() == stylist.strip().lower()]
        if not candidates:
            return {"error": f"Unknown stylist {stylist!r}. Available: {list(INFO.stylists)}."}

    service_lc = service.strip().lower() if service else None
    if service_lc:
        if service_lc not in SERVICE_DURATIONS_MIN:
            return {"error": f"Unknown service {service!r}. Offered: {list(SERVICE_DURATIONS_MIN)}."}
        candidates = [s for s in candidates if service_lc in s.services]
        if not candidates:
            return {"error": f"No stylist on the requested filter offers {service}."}

    duration = SERVICE_DURATIONS_MIN.get(service_lc, 30)

    # Pull that day's bookings in a single query rather than every row.
    with session_scope() as sess:
        rows = sess.scalars(
            select(Appointment).where(Appointment.date == d)
        ).all()
        by_stylist: dict[str, list[tuple[time, time]]] = {}
        for a in rows:
            by_stylist.setdefault(a.staff.name, []).append((a.start_time, a.end_time))

    result = []
    for s in candidates:
        if weekday not in s.schedule:
            continue
        work_start, work_end = s.schedule[weekday]
        win_start = max(work_start, salon_open)
        win_end = min(work_end, salon_close)
        all_slots = _slots_in_range(win_start, win_end)
        their = by_stylist.get(s.name, [])
        avail: list[str] = []
        for start in all_slots:
            end_dt = datetime.combine(d, start) + timedelta(minutes=duration)
            end = end_dt.time()
            if end > win_end:
                continue
            if any(_overlaps(start, end, bs, be) for bs, be in their):
                continue
            avail.append(start.strftime("%H:%M"))
        if avail:
            result.append({"stylist": s.name, "times": avail})

    return {
        "date": date_iso,
        "weekday": weekday,
        "closed": False,
        "service": service_lc,
        "duration_minutes": duration,
        "available": result,
    }


# ---------- Tool: book_appointment ----------


async def book_appointment(
    customer_name: str,
    customer_phone: str,
    stylist: str,
    service: str,
    date_iso: str,
    time_str: str,
    session_id: str | None = None,
    caller_phone: str | None = None,
) -> dict:
    """Book and persist an appointment after re-checking availability.

    ``caller_phone``, when provided, takes precedence over
    ``customer_phone`` for the client link — Twilio told us the true
    number the call is coming from, whereas the LLM may have parsed the
    caller's spoken digits imperfectly. We still store the LLM-captured
    ``customer_phone`` on the appointment row so the historical record
    reflects what was actually said.
    """
    async with _async_lock:
        return await asyncio.to_thread(
            _book_appointment_sync,
            customer_name, customer_phone, stylist, service, date_iso, time_str,
            session_id, caller_phone,
        )


def _book_appointment_sync(
    customer_name: str,
    customer_phone: str,
    stylist: str,
    service: str,
    date_iso: str,
    time_str: str,
    session_id: str | None = None,
    caller_phone: str | None = None,
) -> dict:
    try:
        d = _parse_date(date_iso)
        start = _parse_clock(time_str)
    except ValueError as e:
        return {"error": str(e)}

    stylist_obj = next(
        (s for s in INFO.stylists.values() if s.name.lower() == stylist.strip().lower()),
        None,
    )
    if not stylist_obj:
        return {"error": f"Unknown stylist {stylist!r}."}

    service_lc = service.strip().lower()
    if service_lc not in SERVICE_DURATIONS_MIN:
        return {"error": f"Unknown service {service!r}."}
    if service_lc not in stylist_obj.services:
        return {
            "error": f"{stylist_obj.name} does not offer {service}. "
                     f"They offer: {', '.join(sorted(stylist_obj.services))}."
        }

    weekday = _weekday_name(d)
    if INFO.hours.get(weekday) is None:
        return {"error": f"Salon is closed on {weekday}s."}
    salon_open, salon_close = INFO.hours[weekday]

    duration = SERVICE_DURATIONS_MIN[service_lc]
    end_dt = datetime.combine(d, start) + timedelta(minutes=duration)
    end = end_dt.time()

    if weekday not in stylist_obj.schedule:
        return {"error": f"{stylist_obj.name} doesn't work on {weekday}s."}
    work_start, work_end = stylist_obj.schedule[weekday]
    if start < work_start or end > work_end:
        return {
            "error": f"{stylist_obj.name} works "
                     f"{work_start.strftime('%I:%M %p')} - {work_end.strftime('%I:%M %p')} "
                     f"on {weekday}s. {duration}-minute appointment doesn't fit."
        }
    if start < salon_open or end > salon_close:
        return {"error": "Time is outside salon open hours."}

    with _lock:
        with session_scope() as sess:
            staff_row = sess.scalar(select(StaffRow).where(StaffRow.name == stylist_obj.name))
            svc_row = sess.scalar(
                select(ServiceRow).where(ServiceRow.name.ilike(service_lc))
            )
            if not staff_row or not svc_row:
                # INFO cache said they existed; the DB says otherwise. Someone
                # must have just deleted them in the admin UI. Reload and bail.
                return {"error": "Stylist or service was just removed. Please try again."}

            # Capture display fields before the session closes — the row
            # objects are detached from the session outside this block.
            svc_display = svc_row.name

            # Conflict check inside the same transaction, so a racing admin
            # insert on this stylist's slot can't sneak past us.
            conflicts = sess.scalars(
                select(Appointment).where(
                    Appointment.date == d,
                    Appointment.staff_id == staff_row.id,
                )
            ).all()
            for a in conflicts:
                if _overlaps(start, end, a.start_time, a.end_time):
                    return {"error": f"{stylist_obj.name} is already booked at {time_str}."}

            # Ensure a Clients row exists for this caller. Prefer the
            # Twilio-provided caller_phone (ground truth) over what the
            # LLM captured. Same transaction as the appointment insert,
            # so a failed appointment doesn't strand a client row.
            client_phone_normalized = normalize_phone(caller_phone or customer_phone)
            client_id: int | None = None
            if client_phone_normalized:
                client_row = sess.scalar(
                    select(ClientRow).where(ClientRow.phone == client_phone_normalized)
                )
                first_name = ""
                last_name = ""
                if customer_name:
                    parts = customer_name.strip().split(maxsplit=1)
                    first_name = parts[0] if parts else ""
                    last_name = parts[1] if len(parts) > 1 else ""

                now = datetime.now()
                if client_row is None:
                    client_row = ClientRow(
                        first_name=first_name,
                        last_name=last_name,
                        phone=client_phone_normalized,
                        created_at=now,
                        updated_at=now,
                    )
                    sess.add(client_row)
                    sess.flush()
                else:
                    # Never overwrite existing names — admin edits win.
                    if first_name and not client_row.first_name:
                        client_row.first_name = first_name
                        client_row.updated_at = now
                    if last_name and not client_row.last_name:
                        client_row.last_name = last_name
                        client_row.updated_at = now
                client_id = client_row.id

            appt_id = uuid.uuid4().hex[:8]
            sess.add(Appointment(
                id=appt_id,
                created_at=datetime.now(),
                customer_name=customer_name,
                customer_phone=customer_phone,
                staff_id=staff_row.id,
                service_id=svc_row.id,
                client_id=client_id,
                date=d,
                start_time=start,
                end_time=end,
                session_id=session_id,
            ))

    return {
        "ok": True,
        "appointment_id": appt_id,
        "summary": (
            f"{svc_display} with {stylist_obj.name} on {weekday} "
            f"{date_iso} at {start.strftime('%I:%M %p').lstrip('0')} ({duration} minutes)."
        ),
    }


# ==================== Admin CRUD API ====================
#
# Everything below is synchronous. The API layer runs each one in a thread
# and calls `reload()` after any mutation.


# ---------- Staff ----------


def list_staff() -> list[dict]:
    """Return all staff as JSON-friendly dicts."""
    out = []
    for name, s in INFO.stylists.items():
        out.append({
            "name": name,
            "services": [svc for svc in s.services],
            "schedule": {
                day: {
                    "start": start.strftime("%H:%M"),
                    "end": end.strftime("%H:%M"),
                }
                for day, (start, end) in s.schedule.items()
            },
        })
    return out


def _validate_staff_payload(s: dict) -> None:
    if not isinstance(s.get("name"), str) or not s["name"].strip():
        raise ValueError("Staff name is required.")
    if not isinstance(s.get("services", []), list):
        raise ValueError("services must be a list.")
    schedule = s.get("schedule", {})
    if not isinstance(schedule, dict):
        raise ValueError("schedule must be an object keyed by weekday.")
    for day, slot in schedule.items():
        if day not in WEEKDAYS:
            raise ValueError(f"Unknown weekday {day!r}. Use one of {WEEKDAYS}.")
        if slot is None:
            continue
        if not isinstance(slot, dict):
            raise ValueError(f"Schedule for {day} must be an object with start/end.")
        start = slot.get("start")
        end = slot.get("end")
        if not start and not end:
            continue
        try:
            st = _parse_clock(str(start))
            en = _parse_clock(str(end))
        except Exception as e:
            raise ValueError(f"Invalid time in {day} schedule: {e}") from e
        if st >= en:
            raise ValueError(f"{day} start must be before end.")


def _normalize_staff(s: dict) -> dict:
    """Normalize the payload we persist: lowercase service names, drop empties."""
    services = [str(x).strip().lower() for x in s.get("services", []) if str(x).strip()]
    schedule: dict[str, dict[str, str]] = {}
    for day, slot in (s.get("schedule") or {}).items():
        if not slot:
            continue
        start = slot.get("start")
        end = slot.get("end")
        if not start or not end:
            continue
        schedule[day] = {"start": start, "end": end}
    return {"name": s["name"].strip(), "services": services, "schedule": schedule}


def _apply_staff_payload(sess: Session, staff: StaffRow, payload: dict) -> None:
    """Overwrite ``staff``'s services and schedule from a normalized payload."""
    staff.name = payload["name"]

    # Services — resolve names to Service rows. Unknown names are errors
    # rather than silent skips so a typo in the admin UI surfaces fast.
    wanted_lc = payload["services"]
    if wanted_lc:
        svc_rows = sess.scalars(
            select(ServiceRow).where(
                ServiceRow.name.in_([n.title() for n in wanted_lc])
                | ServiceRow.name.in_(wanted_lc)
            )
        ).all()
        found_lc = {r.name.lower() for r in svc_rows}
        missing = [n for n in wanted_lc if n not in found_lc]
        if missing:
            raise ValueError(f"Unknown service(s): {', '.join(missing)}.")
        staff.services = list(svc_rows)
    else:
        staff.services = []

    # Schedule — drop and rebuild. Simpler than diffing, and the row count
    # per staff member maxes out at 7.
    for h in list(staff.hours):
        sess.delete(h)
    sess.flush()  # actually delete rows before we insert to avoid UNIQUE clash
    for day, slot in payload["schedule"].items():
        staff.hours.append(StaffHoursRow(
            weekday=day,
            start=_parse_clock(slot["start"]),
            end=_parse_clock(slot["end"]),
        ))


def create_staff(payload: dict) -> dict:
    _validate_staff_payload(payload)
    payload = _normalize_staff(payload)
    with _lock:
        try:
            with session_scope() as sess:
                existing = sess.scalar(
                    select(StaffRow).where(StaffRow.name.ilike(payload["name"]))
                )
                if existing:
                    raise ValueError(f"Staff member {payload['name']!r} already exists.")
                staff = StaffRow(name=payload["name"])
                sess.add(staff)
                sess.flush()
                _apply_staff_payload(sess, staff, payload)
        except IntegrityError as e:
            raise ValueError(f"Staff member {payload['name']!r} already exists.") from e
    reload()
    return payload


def update_staff(name: str, payload: dict) -> dict:
    _validate_staff_payload(payload)
    payload = _normalize_staff(payload)
    with _lock:
        with session_scope() as sess:
            staff = sess.scalar(select(StaffRow).where(StaffRow.name.ilike(name)))
            if staff is None:
                raise KeyError(f"Staff member {name!r} not found.")
            if payload["name"].lower() != staff.name.lower():
                clash = sess.scalar(
                    select(StaffRow).where(StaffRow.name.ilike(payload["name"]))
                )
                if clash is not None:
                    raise ValueError(f"Staff member {payload['name']!r} already exists.")
            _apply_staff_payload(sess, staff, payload)
    reload()
    return payload


def delete_staff(name: str) -> None:
    with _lock:
        with session_scope() as sess:
            staff = sess.scalar(select(StaffRow).where(StaffRow.name.ilike(name)))
            if staff is None:
                raise KeyError(f"Staff member {name!r} not found.")
            # RESTRICT would also block on historical appointments (which we
            # want to keep). Surface a friendly error so the admin knows to
            # delete or reassign them first, rather than seeing an
            # IntegrityError bubble up as an opaque 500.
            has_any = sess.scalar(
                select(Appointment.id).where(Appointment.staff_id == staff.id).limit(1)
            )
            if has_any:
                raise ValueError(
                    f"Cannot delete {staff.name!r}: they still have appointments on file. "
                    f"Delete or reassign those appointments first."
                )
            sess.delete(staff)
    reload()


# ---------- Services ----------


def list_services() -> list[dict]:
    return [
        {"name": svc.name, "duration_minutes": svc.duration_minutes, "price": svc.price}
        for svc in INFO.services.values()
    ]


def _validate_service_payload(s: dict) -> None:
    if not isinstance(s.get("name"), str) or not s["name"].strip():
        raise ValueError("Service name is required.")
    try:
        duration = int(s.get("duration_minutes"))
    except (TypeError, ValueError) as e:
        raise ValueError("duration_minutes must be an integer.") from e
    if duration <= 0 or duration % SLOT_MIN != 0:
        raise ValueError(
            f"duration_minutes must be a positive multiple of {SLOT_MIN}."
        )
    try:
        price = float(s.get("price"))
    except (TypeError, ValueError) as e:
        raise ValueError("price must be a number.") from e
    if price < 0:
        raise ValueError("price must be non-negative.")


def _normalize_service(s: dict) -> dict:
    return {
        "name": s["name"].strip().title(),
        "duration_minutes": int(s["duration_minutes"]),
        "price": float(s["price"]),
    }


def create_service(payload: dict) -> dict:
    _validate_service_payload(payload)
    payload = _normalize_service(payload)
    with _lock:
        try:
            with session_scope() as sess:
                existing = sess.scalar(
                    select(ServiceRow).where(ServiceRow.name.ilike(payload["name"]))
                )
                if existing:
                    raise ValueError(f"Service {payload['name']!r} already exists.")
                sess.add(ServiceRow(
                    name=payload["name"],
                    duration_minutes=payload["duration_minutes"],
                    price=payload["price"],
                ))
        except IntegrityError as e:
            raise ValueError(f"Service {payload['name']!r} already exists.") from e
    reload()
    return payload


def update_service(name: str, payload: dict) -> dict:
    _validate_service_payload(payload)
    payload = _normalize_service(payload)
    with _lock:
        with session_scope() as sess:
            svc = sess.scalar(select(ServiceRow).where(ServiceRow.name.ilike(name)))
            if svc is None:
                raise KeyError(f"Service {name!r} not found.")
            if payload["name"].lower() != svc.name.lower():
                clash = sess.scalar(
                    select(ServiceRow).where(ServiceRow.name.ilike(payload["name"]))
                )
                if clash is not None:
                    raise ValueError(f"Service {payload['name']!r} already exists.")
            svc.name = payload["name"]
            svc.duration_minutes = payload["duration_minutes"]
            svc.price = payload["price"]
    reload()
    return payload


def delete_service(name: str) -> None:
    with _lock:
        with session_scope() as sess:
            svc = sess.scalar(select(ServiceRow).where(ServiceRow.name.ilike(name)))
            if svc is None:
                raise KeyError(f"Service {name!r} not found.")
            has_any = sess.scalar(
                select(Appointment.id).where(Appointment.service_id == svc.id).limit(1)
            )
            if has_any:
                raise ValueError(
                    f"Cannot delete {svc.name!r}: it is used by existing appointments. "
                    f"Delete those appointments first."
                )
            sess.delete(svc)
    reload()


# ---------- Hours & location ----------


def get_hours() -> dict[str, dict | None]:
    """Weekday -> {'open': 'HH:MM', 'close': 'HH:MM'} or None when closed."""
    out: dict[str, dict | None] = {}
    for day in WEEKDAYS:
        h = INFO.hours.get(day)
        if h is None:
            out[day] = None
        else:
            out[day] = {"open": h[0].strftime("%H:%M"), "close": h[1].strftime("%H:%M")}
    return out


def update_hours(payload: dict[str, dict | None]) -> dict:
    """Overwrite all hours rows from a {day: {open, close} | None} dict."""
    parsed: dict[str, tuple[time, time] | None] = {}
    for day in WEEKDAYS:
        slot = payload.get(day) if isinstance(payload, dict) else None
        if slot is None:
            parsed[day] = None
            continue
        try:
            o = _parse_clock(str(slot["open"]))
            c = _parse_clock(str(slot["close"]))
        except Exception as e:
            raise ValueError(f"Invalid hours for {day}: {e}") from e
        if o >= c:
            raise ValueError(f"{day}: open time must be before close time.")
        parsed[day] = (o, c)

    with _lock:
        with session_scope() as sess:
            # Wipe and re-insert. Seven rows; no point in a diff.
            sess.execute(delete(HoursRow))
            for day in WEEKDAYS:
                slot = parsed[day]
                if slot is None:
                    sess.add(HoursRow(weekday=day, open=None, close=None))
                else:
                    sess.add(HoursRow(weekday=day, open=slot[0], close=slot[1]))
    reload()
    return get_hours()


def get_location() -> str:
    return INFO.location


def update_location(location: str) -> str:
    if not isinstance(location, str):
        raise ValueError("location must be a string.")
    with _lock:
        with session_scope() as sess:
            salon_row = sess.scalar(select(SalonRow).order_by(SalonRow.id).limit(1))
            if salon_row is None:
                sess.add(SalonRow(id=1, location=location.strip()))
            else:
                salon_row.location = location.strip()
    reload()
    return get_location()


# ---------- Appointments (admin) ----------


def list_appointments(start: str | None = None, end: str | None = None) -> list[dict]:
    """Return appointments in [start, end] (inclusive). Both are optional."""
    start_d = _parse_date(start) if start else None
    end_d = _parse_date(end) if end else None
    with session_scope() as sess:
        stmt = select(Appointment).order_by(Appointment.date, Appointment.start_time)
        if start_d is not None:
            stmt = stmt.where(Appointment.date >= start_d)
        if end_d is not None:
            stmt = stmt.where(Appointment.date <= end_d)
        return [_appt_to_dict(a) for a in sess.scalars(stmt)]


def _validate_appointment_payload(a: dict, *, require_id: bool = False) -> None:
    if require_id and not a.get("id"):
        raise ValueError("id is required.")
    for k in ("customer_name", "customer_phone", "stylist", "service", "date", "start_time"):
        if not a.get(k):
            raise ValueError(f"{k} is required.")
    try:
        _parse_date(str(a["date"]))
    except ValueError as e:
        raise ValueError(f"Invalid date: {e}") from e
    try:
        _parse_clock(str(a["start_time"]))
    except ValueError as e:
        raise ValueError(f"Invalid start_time: {e}") from e


def _end_from_service(service: str, start_time: str) -> str:
    service_lc = service.strip().lower()
    duration = SERVICE_DURATIONS_MIN.get(service_lc)
    if duration is None:
        raise ValueError(f"Unknown service {service!r}.")
    start = _parse_clock(start_time)
    end_dt = datetime.combine(date.today(), start) + timedelta(minutes=duration)
    return end_dt.time().strftime("%H:%M")


def _resolve_staff_and_service(
    sess: Session, stylist_name: str, service_name: str
) -> tuple[StaffRow, ServiceRow]:
    staff = sess.scalar(select(StaffRow).where(StaffRow.name.ilike(stylist_name.strip())))
    if staff is None:
        raise ValueError(f"Unknown stylist {stylist_name!r}.")
    svc = sess.scalar(select(ServiceRow).where(ServiceRow.name.ilike(service_name.strip())))
    if svc is None:
        raise ValueError(f"Unknown service {service_name!r}.")
    return staff, svc


def _admin_conflict_query(
    sess: Session,
    *,
    when: date,
    staff_id: int,
    start: time,
    end: time,
    exclude_id: str | None = None,
) -> None:
    """Raise ValueError if any other appointment overlaps this slot."""
    stmt = select(Appointment).where(
        Appointment.date == when,
        Appointment.staff_id == staff_id,
    )
    if exclude_id is not None:
        stmt = stmt.where(Appointment.id != exclude_id)
    for a in sess.scalars(stmt):
        if _overlaps(start, end, a.start_time, a.end_time):
            raise ValueError(
                f"{a.staff.name} is already booked at "
                f"{a.start_time.strftime('%H:%M')}-{a.end_time.strftime('%H:%M')} "
                f"on {when.isoformat()}."
            )


def create_appointment(payload: dict) -> dict:
    _validate_appointment_payload(payload)
    with _lock:
        with session_scope() as sess:
            staff, svc = _resolve_staff_and_service(
                sess, payload["stylist"], payload["service"]
            )
            d = _parse_date(str(payload["date"]))
            start = _parse_clock(payload["start_time"])
            end_str = payload.get("end_time") or _end_from_service(
                payload["service"], payload["start_time"]
            )
            end = _parse_clock(end_str)
            _admin_conflict_query(
                sess, when=d, staff_id=staff.id, start=start, end=end
            )
            appt = Appointment(
                id=uuid.uuid4().hex[:8],
                created_at=datetime.now(),
                customer_name=payload["customer_name"].strip(),
                customer_phone=payload["customer_phone"].strip(),
                staff_id=staff.id,
                service_id=svc.id,
                date=d,
                start_time=start,
                end_time=end,
            )
            sess.add(appt)
            sess.flush()
            sess.refresh(appt)
            return _appt_to_dict(appt)


def update_appointment(appt_id: str, payload: dict) -> dict:
    _validate_appointment_payload(payload)
    with _lock:
        with session_scope() as sess:
            appt = sess.get(Appointment, appt_id)
            if appt is None:
                raise KeyError(f"Appointment {appt_id!r} not found.")
            staff, svc = _resolve_staff_and_service(
                sess, payload["stylist"], payload["service"]
            )
            d = _parse_date(str(payload["date"]))
            start = _parse_clock(payload["start_time"])
            end_str = payload.get("end_time") or _end_from_service(
                payload["service"], payload["start_time"]
            )
            end = _parse_clock(end_str)
            _admin_conflict_query(
                sess, when=d, staff_id=staff.id, start=start, end=end,
                exclude_id=appt_id,
            )
            appt.customer_name = payload["customer_name"].strip()
            appt.customer_phone = payload["customer_phone"].strip()
            appt.staff_id = staff.id
            appt.service_id = svc.id
            appt.date = d
            appt.start_time = start
            appt.end_time = end
            sess.flush()
            sess.refresh(appt)
            return _appt_to_dict(appt)


def delete_appointment(appt_id: str) -> None:
    with _lock:
        with session_scope() as sess:
            appt = sess.get(Appointment, appt_id)
            if appt is None:
                raise KeyError(f"Appointment {appt_id!r} not found.")
            sess.delete(appt)


# ---------- Clients (Phase 2) ----------
#
# The bot uses ``get_client_by_phone`` on every call; admin CRUD is
# exposed through /api/clients. Clients are keyed by normalized phone
# (see ``normalize_phone``).


_ALLOWED_GENDERS = ("male", "female", "nonbinary", "unspecified")


def _client_to_dict(c: ClientRow) -> dict:
    return {
        "id": c.id,
        "first_name": c.first_name,
        "last_name": c.last_name,
        "phone": c.phone,
        "phone_formatted": format_phone(c.phone),
        "email": c.email,
        "gender": c.gender,
        "notes": c.notes,
        "created_at": c.created_at.isoformat(timespec="seconds"),
        "updated_at": c.updated_at.isoformat(timespec="seconds"),
    }


def _validate_client_payload(c: dict) -> None:
    if not isinstance(c.get("first_name"), str) and c.get("first_name") is not None:
        raise ValueError("first_name must be a string.")
    if not isinstance(c.get("last_name"), str) and c.get("last_name") is not None:
        raise ValueError("last_name must be a string.")
    phone = c.get("phone")
    if not phone or not str(phone).strip():
        raise ValueError("phone is required.")
    if not normalize_phone(str(phone)):
        raise ValueError(
            f"phone {phone!r} doesn't look like a US number "
            f"(need at least 10 digits)."
        )
    gender = c.get("gender")
    if gender and gender not in _ALLOWED_GENDERS:
        raise ValueError(
            f"gender must be one of {_ALLOWED_GENDERS}, got {gender!r}."
        )


def _normalize_client(c: dict) -> dict:
    """Trim strings, normalize phone, coerce empties to None where the column is nullable."""
    def _s(k: str) -> str:
        v = c.get(k)
        return str(v).strip() if v is not None else ""

    def _opt(k: str) -> str | None:
        v = c.get(k)
        if v is None:
            return None
        s = str(v).strip()
        return s or None

    return {
        "first_name": _s("first_name"),
        "last_name": _s("last_name"),
        "phone": normalize_phone(str(c.get("phone") or "")),
        "email": _opt("email"),
        "gender": _opt("gender"),
        "notes": _opt("notes"),
    }


def list_clients(query: str | None = None) -> list[dict]:
    """Return all clients, most recently updated first.

    ``query``, when passed, does a case-insensitive substring match on
    first/last name and a digits-only substring match on phone. Small
    salons will never have more than a few thousand clients, so a full
    scan is fine; when this stops being fine we'll add a FTS index.
    """
    with session_scope() as sess:
        rows = sess.scalars(
            select(ClientRow).order_by(ClientRow.updated_at.desc())
        ).all()

        if not query:
            return [_client_to_dict(c) for c in rows]

        # Convert inside the session — otherwise commit expires the
        # attributes and _client_to_dict trips DetachedInstanceError.
        q_lower = query.strip().lower()
        q_digits = re.sub(r"\D", "", query)
        out: list[dict] = []
        for c in rows:
            name = f"{c.first_name} {c.last_name}".lower()
            if q_lower and q_lower in name:
                out.append(_client_to_dict(c))
            elif q_digits and q_digits in c.phone:
                out.append(_client_to_dict(c))
        return out


def get_client(client_id: int) -> dict | None:
    with session_scope() as sess:
        c = sess.get(ClientRow, client_id)
        return _client_to_dict(c) if c else None


def get_client_by_phone(phone: str) -> dict | None:
    """Look up a client by any phone-shaped string. Returns ``None`` on miss."""
    normalized = normalize_phone(phone)
    if not normalized:
        return None
    with session_scope() as sess:
        c = sess.scalar(select(ClientRow).where(ClientRow.phone == normalized))
        return _client_to_dict(c) if c else None


def client_history(client_id: int, *, upcoming_only: bool = False) -> list[dict]:
    """Return this client's appointments, most recent first.

    Used by both the admin UI (full history on the client detail page)
    and the bot (only ``upcoming_only=True`` for the caller-context
    block so we don't page a huge history into the system prompt).
    """
    with session_scope() as sess:
        stmt = select(Appointment).where(Appointment.client_id == client_id)
        if upcoming_only:
            stmt = stmt.where(Appointment.date >= date.today())
        stmt = stmt.order_by(Appointment.date.desc(), Appointment.start_time.desc())
        return [_appt_to_dict(a) for a in sess.scalars(stmt)]


def create_client(payload: dict) -> dict:
    _validate_client_payload(payload)
    data = _normalize_client(payload)
    now = datetime.now()
    with _lock:
        try:
            with session_scope() as sess:
                existing = sess.scalar(
                    select(ClientRow).where(ClientRow.phone == data["phone"])
                )
                if existing:
                    raise ValueError(
                        f"A client with phone {format_phone(data['phone'])} "
                        f"already exists (id={existing.id})."
                    )
                client = ClientRow(
                    **data,
                    created_at=now,
                    updated_at=now,
                )
                sess.add(client)
                sess.flush()
                return _client_to_dict(client)
        except IntegrityError as e:
            raise ValueError(
                f"A client with phone {format_phone(data['phone'])} already exists."
            ) from e


def update_client(client_id: int, payload: dict) -> dict:
    _validate_client_payload(payload)
    data = _normalize_client(payload)
    with _lock:
        with session_scope() as sess:
            client = sess.get(ClientRow, client_id)
            if client is None:
                raise KeyError(f"Client {client_id!r} not found.")
            if data["phone"] != client.phone:
                clash = sess.scalar(
                    select(ClientRow).where(
                        ClientRow.phone == data["phone"],
                        ClientRow.id != client_id,
                    )
                )
                if clash is not None:
                    raise ValueError(
                        f"Another client already uses phone "
                        f"{format_phone(data['phone'])} (id={clash.id})."
                    )
            for k, v in data.items():
                setattr(client, k, v)
            client.updated_at = datetime.now()
            sess.flush()
            return _client_to_dict(client)


def delete_client(client_id: int) -> None:
    """Delete a client. Their historical appointments survive (client_id -> NULL)."""
    with _lock:
        with session_scope() as sess:
            client = sess.get(ClientRow, client_id)
            if client is None:
                raise KeyError(f"Client {client_id!r} not found.")
            sess.delete(client)


def upsert_client_from_call(
    phone: str,
    *,
    customer_name: str | None = None,
) -> dict | None:
    """Given a caller's phone + optional name, ensure a client row exists.

    Called from ``book_appointment`` so a first-time caller gets a
    Clients row created as a side effect of booking. Returns the client
    dict (existing or new) or ``None`` if the phone couldn't be
    normalized.
    """
    normalized = normalize_phone(phone)
    if not normalized:
        return None

    # Split "First Last" into two columns. Everything after the first
    # space becomes last name; single-word names go entirely into first.
    first = ""
    last = ""
    if customer_name:
        parts = customer_name.strip().split(maxsplit=1)
        first = parts[0] if parts else ""
        last = parts[1] if len(parts) > 1 else ""

    now = datetime.now()
    with _lock:
        with session_scope() as sess:
            client = sess.scalar(select(ClientRow).where(ClientRow.phone == normalized))
            if client is None:
                client = ClientRow(
                    first_name=first,
                    last_name=last,
                    phone=normalized,
                    created_at=now,
                    updated_at=now,
                )
                sess.add(client)
                sess.flush()
            else:
                # Fill in any missing name info from this call. Never
                # overwrite an existing name — the admin's edits win.
                if first and not client.first_name:
                    client.first_name = first
                    client.updated_at = now
                if last and not client.last_name:
                    client.last_name = last
                    client.updated_at = now
            return _client_to_dict(client)


# ---------- Compat re-exports for the import/export CLIs ----------
#
# The xlsx-side helpers below are shared with import_xlsx.py / export_xlsx.py
# so they can round-trip data using the same parsers we use everywhere.


__all__ = [
    "WEEKDAYS",
    "SLOT_MIN",
    "Service",
    "Stylist",
    "SalonInfo",
    "INFO",
    "SERVICE_DURATIONS_MIN",
    "reload",
    "normalize_phone",
    "format_phone",
    "system_prompt_context",
    "caller_context",
    "check_availability",
    "book_appointment",
    "list_staff",
    "create_staff",
    "update_staff",
    "delete_staff",
    "list_services",
    "create_service",
    "update_service",
    "delete_service",
    "get_hours",
    "update_hours",
    "get_location",
    "update_location",
    "list_appointments",
    "create_appointment",
    "update_appointment",
    "delete_appointment",
    "list_clients",
    "get_client",
    "get_client_by_phone",
    "client_history",
    "create_client",
    "update_client",
    "delete_client",
    "upsert_client_from_call",
]
