"""Salon data store: hours, stylists, schedules, and appointment booking.

Reads static salon data (hours, location, stylists, schedules) from the Excel
file at startup. Persists bookings to an "Appointments" sheet in the same file,
creating it on first use.

Exposes two async functions the LLM calls as tools:
  - check_availability(date_iso, stylist?, service?)
  - book_appointment(customer_name, customer_phone, stylist, service, date_iso, time_str)
"""

from __future__ import annotations

import asyncio
import os
import re
import uuid
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path

import openpyxl
from loguru import logger

DEFAULT_XLSX = Path(r"C:\Users\logan\claude-projects\funkle\ReceptionistData.xlsx")
SALON_XLSX = Path(os.getenv("SALON_DATA_PATH", str(DEFAULT_XLSX)))

# Per-service appointment length in minutes. Real salons would tune these.
SERVICE_DURATIONS_MIN = {"cut": 30, "color": 90, "perm": 120, "shave": 30}
SLOT_MIN = 30  # all appointments align to 30-minute slot boundaries

# Excel writes aren't atomic — serialize all booking ops behind a single lock.
_lock = asyncio.Lock()


# ---------- Parsing helpers ----------


def _parse_time(s: str) -> time:
    """Parse '10AM', '12 PM', '7PM' → datetime.time."""
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


def _parse_range(s: str) -> tuple[time, time]:
    """Parse '10AM to 4PM' → (time(10,0), time(16,0))."""
    parts = re.split(r"\s+to\s+", s.strip(), flags=re.IGNORECASE)
    return _parse_time(parts[0]), _parse_time(parts[1])


def _parse_date(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


def _parse_clock(s: str) -> time:
    """Parse '14:30' or '2:30 PM' → datetime.time."""
    s = s.strip().upper()
    if re.search(r"AM|PM", s):
        return _parse_time(s)
    h, m = s.split(":")
    return time(int(h), int(m))


def _weekday_name(d: date) -> str:
    return ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"][d.weekday()]


# ---------- Data model ----------


@dataclass(frozen=True)
class Stylist:
    name: str
    services: tuple[str, ...]  # lowercased service names
    schedule: dict[str, tuple[time, time]]  # weekday → (start, end)


@dataclass
class SalonInfo:
    location: str
    hours: dict[str, tuple[time, time] | None]  # weekday → (open, close), or None when closed
    stylists: dict[str, Stylist]


def _load_info() -> SalonInfo:
    wb = openpyxl.load_workbook(SALON_XLSX, data_only=True)

    # Hours sheet
    hours: dict[str, tuple[time, time] | None] = {}
    for day, open_s, close_s in list(wb["Hours"].iter_rows(values_only=True))[1:]:
        if not day:
            continue
        if (open_s or "").strip().upper() == "NA":
            hours[day] = None
        else:
            hours[day] = (_parse_time(str(open_s)), _parse_time(str(close_s)))

    # Location sheet
    loc_rows = list(wb["Location"].iter_rows(values_only=True))
    location = str(loc_rows[1][1])

    # Associates sheet — stylist → services they offer
    stylist_services: dict[str, tuple[str, ...]] = {}
    for row in list(wb["Associates"].iter_rows(values_only=True))[1:]:
        name = row[0]
        if not name:
            continue
        services = tuple(s.strip().lower() for s in row[1:] if s and isinstance(s, str))
        stylist_services[name.strip()] = services

    # Schedule sheet — stylist → weekday → (start, end)
    sched_rows = list(wb["Schedule"].iter_rows(values_only=True))
    day_headers = [d for d in sched_rows[0][1:] if d]
    schedules: dict[str, dict[str, tuple[time, time]]] = {}
    for row in sched_rows[1:]:
        name = row[0]
        if not name:
            continue
        sched: dict[str, tuple[time, time]] = {}
        for day, cell in zip(day_headers, row[1:]):
            if cell and isinstance(cell, str) and cell.strip():
                sched[day] = _parse_range(cell)
        schedules[name.strip()] = sched

    stylists = {
        name: Stylist(name=name, services=services, schedule=schedules.get(name, {}))
        for name, services in stylist_services.items()
    }
    return SalonInfo(location=location, hours=hours, stylists=stylists)


INFO = _load_info()
logger.info(
    f"Salon loaded from {SALON_XLSX.name}: "
    f"{len(INFO.stylists)} stylists ({', '.join(INFO.stylists)})"
)


# ---------- Appointments sheet (R/W) ----------


_APPT_COLUMNS = [
    "id", "created_at", "customer_name", "customer_phone",
    "stylist", "service", "date", "start_time", "end_time",
]


def _ensure_appointments_sheet() -> None:
    wb = openpyxl.load_workbook(SALON_XLSX)
    if "Appointments" not in wb.sheetnames:
        ws = wb.create_sheet("Appointments")
        ws.append(_APPT_COLUMNS)
        wb.save(SALON_XLSX)


_ensure_appointments_sheet()


def _read_appointments() -> list[dict]:
    wb = openpyxl.load_workbook(SALON_XLSX, data_only=True)
    ws = wb["Appointments"]
    rows = list(ws.iter_rows(values_only=True))
    if len(rows) < 2:
        return []
    return [dict(zip(_APPT_COLUMNS, row)) for row in rows[1:] if row and row[0]]


def _append_appointment(appt: dict) -> None:
    wb = openpyxl.load_workbook(SALON_XLSX)
    ws = wb["Appointments"]
    ws.append([appt[k] for k in _APPT_COLUMNS])
    wb.save(SALON_XLSX)


# ---------- Public salon-context helper for the system prompt ----------


def system_prompt_context() -> str:
    """Render salon static info as a block to inject into the LLM system prompt."""
    lines = ["SALON LOCATION:", f"  {INFO.location}", "", "SALON HOURS:"]
    for day in ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]:
        h = INFO.hours.get(day)
        if h is None:
            lines.append(f"  {day}: closed")
        else:
            lines.append(f"  {day}: {h[0].strftime('%-I %p').lstrip('0') if os.name != 'nt' else h[0].strftime('%#I %p')} to {h[1].strftime('%#I %p') if os.name == 'nt' else h[1].strftime('%-I %p').lstrip('0')}")

    lines += ["", "STYLISTS AND SERVICES:"]
    for name, s in INFO.stylists.items():
        services = ", ".join(sorted(s.services))
        lines.append(f"  {name}: {services}")

    lines += ["", "STYLIST SCHEDULES:"]
    for name, s in INFO.stylists.items():
        days = []
        for day in ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]:
            if day in s.schedule:
                start, end = s.schedule[day]
                days.append(
                    f"{day[:3]} "
                    f"{start.strftime('%#I:%M%p' if os.name == 'nt' else '%-I:%M%p')}"
                    f"-{end.strftime('%#I:%M%p' if os.name == 'nt' else '%-I:%M%p')}"
                )
        lines.append(f"  {name}: {', '.join(days) if days else 'not scheduled'}")

    lines += ["", "SERVICE DURATIONS (minutes):"]
    for service, mins in SERVICE_DURATIONS_MIN.items():
        lines.append(f"  {service}: {mins}")

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
    """Return open appointment slots on the given date, optionally filtered.

    Args:
        date_iso: ISO date, e.g. "2026-06-25".
        stylist: Optional stylist name to filter by.
        service: Optional service name (cut/color/perm/shave) to filter by.

    Returns:
        On success: {"date", "weekday", "closed", "service", "duration_minutes",
                     "available": [{"stylist", "times": ["HH:MM", ...]}, ...]}
        On failure: {"error": "..."}
    """
    async with _lock:
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

    # Pre-load existing bookings for the day.
    booked = _read_appointments()
    by_stylist: dict[str, list[tuple[time, time]]] = {}
    for a in booked:
        if str(a["date"]) != date_iso:
            continue
        by_stylist.setdefault(a["stylist"], []).append(
            (_parse_clock(str(a["start_time"])), _parse_clock(str(a["end_time"])))
        )

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
            # Slot must fit within working window
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
) -> dict:
    """Book and persist an appointment after re-checking availability."""
    async with _lock:
        return await asyncio.to_thread(
            _book_appointment_sync,
            customer_name, customer_phone, stylist, service, date_iso, time_str,
        )


def _book_appointment_sync(
    customer_name: str,
    customer_phone: str,
    stylist: str,
    service: str,
    date_iso: str,
    time_str: str,
) -> dict:
    try:
        d = _parse_date(date_iso)
        start = _parse_clock(time_str)
    except ValueError as e:
        return {"error": str(e)}

    # Resolve stylist (case-insensitive)
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

    # Conflict check
    for a in _read_appointments():
        if str(a["date"]) != date_iso or a["stylist"] != stylist_obj.name:
            continue
        ostart = _parse_clock(str(a["start_time"]))
        oend = _parse_clock(str(a["end_time"]))
        if _overlaps(start, end, ostart, oend):
            return {"error": f"{stylist_obj.name} is already booked at {time_str}."}

    appt_id = uuid.uuid4().hex[:8]
    _append_appointment({
        "id": appt_id,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "customer_name": customer_name,
        "customer_phone": customer_phone,
        "stylist": stylist_obj.name,
        "service": service_lc.title(),
        "date": date_iso,
        "start_time": start.strftime("%H:%M"),
        "end_time": end.strftime("%H:%M"),
    })

    return {
        "ok": True,
        "appointment_id": appt_id,
        "summary": (
            f"{service_lc.title()} with {stylist_obj.name} on {weekday} "
            f"{date_iso} at {start.strftime('%I:%M %p').lstrip('0')} ({duration} minutes)."
        ),
    }
