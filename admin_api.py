"""REST endpoints for the admin UI.

All mutations are dispatched through `asyncio.to_thread` because the salon
data layer uses blocking openpyxl reads/writes. Everything under `/api` here
returns JSON and speaks in the plain dicts the salon module accepts.
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

import calls
import salon

router = APIRouter(prefix="/api", tags=["admin"])


# ---------- Request models ----------


class ScheduleSlot(BaseModel):
    start: str
    end: str


class StaffPayload(BaseModel):
    name: str
    services: list[str] = Field(default_factory=list)
    schedule: dict[str, ScheduleSlot | None] = Field(default_factory=dict)


class ServicePayload(BaseModel):
    name: str
    duration_minutes: int
    price: float


class HoursSlot(BaseModel):
    open: str
    close: str


class HoursPayload(BaseModel):
    hours: dict[str, HoursSlot | None]


class LocationPayload(BaseModel):
    location: str


class AppointmentPayload(BaseModel):
    customer_name: str
    customer_phone: str
    stylist: str
    service: str
    date: str
    start_time: str
    end_time: str | None = None


# ---------- Helpers ----------


async def _run(fn, *args, **kwargs) -> Any:
    """Dispatch a blocking salon helper onto a worker thread."""
    return await asyncio.to_thread(fn, *args, **kwargs)


def _client_error(e: Exception) -> HTTPException:
    return HTTPException(status_code=400, detail=str(e))


def _not_found(e: Exception) -> HTTPException:
    return HTTPException(status_code=404, detail=str(e))


# ---------- Meta ----------


@router.get("/summary")
async def summary() -> dict:
    """Cheap health/status endpoint used by the UI's dashboard tile."""
    return {
        "location": salon.get_location(),
        "staff_count": len(salon.INFO.stylists),
        "service_count": len(salon.INFO.services),
        "appointment_count": len(await _run(salon.list_appointments)),
        "call_count": len(await _run(calls.list_calls)),
    }


# ---------- Calls (observability) ----------


@router.get("/calls")
async def get_calls() -> list[dict]:
    return await _run(calls.list_calls)


@router.get("/calls/{session_id}")
async def get_call_detail(session_id: str) -> dict:
    call = await _run(calls.get_call, session_id)
    if call is None:
        raise HTTPException(status_code=404, detail=f"Call {session_id!r} not found.")
    return call


# ---------- Staff ----------


@router.get("/staff")
async def get_staff() -> list[dict]:
    return await _run(salon.list_staff)


@router.post("/staff")
async def add_staff(payload: StaffPayload) -> dict:
    try:
        return await _run(salon.create_staff, payload.model_dump())
    except ValueError as e:
        raise _client_error(e)


@router.put("/staff/{name}")
async def edit_staff(name: str, payload: StaffPayload) -> dict:
    try:
        return await _run(salon.update_staff, name, payload.model_dump())
    except KeyError as e:
        raise _not_found(e)
    except ValueError as e:
        raise _client_error(e)


@router.delete("/staff/{name}")
async def remove_staff(name: str) -> dict:
    try:
        await _run(salon.delete_staff, name)
    except KeyError as e:
        raise _not_found(e)
    return {"ok": True}


# ---------- Services ----------


@router.get("/services")
async def get_services() -> list[dict]:
    return await _run(salon.list_services)


@router.post("/services")
async def add_service(payload: ServicePayload) -> dict:
    try:
        return await _run(salon.create_service, payload.model_dump())
    except ValueError as e:
        raise _client_error(e)


@router.put("/services/{name}")
async def edit_service(name: str, payload: ServicePayload) -> dict:
    try:
        return await _run(salon.update_service, name, payload.model_dump())
    except KeyError as e:
        raise _not_found(e)
    except ValueError as e:
        raise _client_error(e)


@router.delete("/services/{name}")
async def remove_service(name: str) -> dict:
    try:
        await _run(salon.delete_service, name)
    except KeyError as e:
        raise _not_found(e)
    return {"ok": True}


# ---------- Hours & location ----------


@router.get("/hours")
async def get_hours() -> dict:
    return await _run(salon.get_hours)


@router.put("/hours")
async def put_hours(payload: HoursPayload) -> dict:
    body = {day: (slot.model_dump() if slot else None)
            for day, slot in payload.hours.items()}
    try:
        return await _run(salon.update_hours, body)
    except ValueError as e:
        raise _client_error(e)


@router.get("/location")
async def get_location() -> dict:
    return {"location": salon.get_location()}


@router.put("/location")
async def put_location(payload: LocationPayload) -> dict:
    try:
        return {"location": await _run(salon.update_location, payload.location)}
    except ValueError as e:
        raise _client_error(e)


# ---------- Appointments ----------


@router.get("/appointments")
async def get_appointments(
    start: str | None = Query(default=None),
    end: str | None = Query(default=None),
) -> list[dict]:
    try:
        return await _run(salon.list_appointments, start, end)
    except ValueError as e:
        raise _client_error(e)


@router.post("/appointments")
async def add_appointment(payload: AppointmentPayload) -> dict:
    try:
        return await _run(salon.create_appointment, payload.model_dump())
    except ValueError as e:
        raise _client_error(e)


@router.put("/appointments/{appt_id}")
async def edit_appointment(appt_id: str, payload: AppointmentPayload) -> dict:
    try:
        return await _run(salon.update_appointment, appt_id, payload.model_dump())
    except KeyError as e:
        raise _not_found(e)
    except ValueError as e:
        raise _client_error(e)


@router.delete("/appointments/{appt_id}")
async def remove_appointment(appt_id: str) -> dict:
    try:
        await _run(salon.delete_appointment, appt_id)
    except KeyError as e:
        raise _not_found(e)
    return {"ok": True}
