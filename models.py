"""SQLAlchemy models for the salon data store.

The schema mirrors what the xlsx workbook expressed via sheets, plus a
``Client`` table added in Phase 2:

  Location sheet   -> Salon (single row)
  Hours sheet      -> Hours (one row per weekday)
  Services sheet   -> Service
  Associates sheet -> Staff + StaffService (many-to-many)
  Schedule sheet   -> StaffHours (one row per staff × weekday)
  Appointments     -> Appointment  (now with optional client_id FK)
  (new)            -> Client       (people we've talked to before)

Design notes:

- Appointment IDs stay as 8-char hex strings (matches the pre-DB shape the
  admin UI and voice bot already emit). A future migration can flip them
  to integer surrogate keys if we want; not worth the churn now.
- Staff and Service are referenced by FK from Appointment, not by name.
  This means renaming a stylist doesn't require touching a thousand
  appointment rows.
- ``ON DELETE RESTRICT`` on the Appointment FKs: deleting a stylist or
  service that still has appointments now fails loudly (the xlsx version
  silently orphaned them, which was a latent bug).
- All weekday strings use the canonical Sunday..Saturday form defined in
  ``salon.WEEKDAYS`` — no enum needed since SQLite doesn't enforce them.
- Client.phone stores the *normalized* form (US: last 10 digits). Lookups
  and uniqueness both operate on that form; the raw E.164 the caller
  actually dialed lives in Appointment.customer_phone.
"""

from __future__ import annotations

from datetime import date, datetime, time

from sqlalchemy import (
    Column,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Time,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Salon(Base):
    """Single-row table (id=1). Holds the salon's location string.

    A separate table (rather than a JSON column on Hours or a constants
    module) keeps the ``update_location`` transaction atomic and easy
    to reason about.
    """

    __tablename__ = "salons"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    location: Mapped[str] = mapped_column(String, nullable=False, default="")


class Hours(Base):
    """One row per weekday. ``open``/``close`` are NULL on closed days."""

    __tablename__ = "hours"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    weekday: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    open: Mapped[time | None] = mapped_column(Time, nullable=True)
    close: Mapped[time | None] = mapped_column(Time, nullable=True)


class Service(Base):
    __tablename__ = "services"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    duration_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    price: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)

    staff: Mapped[list["Staff"]] = relationship(
        "Staff",
        secondary="staff_services",
        back_populates="services",
        viewonly=False,
    )


class Staff(Base):
    __tablename__ = "staff"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False, unique=True)

    services: Mapped[list[Service]] = relationship(
        Service,
        secondary="staff_services",
        back_populates="staff",
        viewonly=False,
    )
    hours: Mapped[list["StaffHours"]] = relationship(
        "StaffHours",
        cascade="all, delete-orphan",
        back_populates="staff",
    )


class StaffService(Base):
    """Many-to-many join between staff and services."""

    __tablename__ = "staff_services"

    staff_id: Mapped[int] = mapped_column(
        ForeignKey("staff.id", ondelete="CASCADE"), primary_key=True
    )
    service_id: Mapped[int] = mapped_column(
        ForeignKey("services.id", ondelete="CASCADE"), primary_key=True
    )


class StaffHours(Base):
    """Weekly recurring schedule for a staff member."""

    __tablename__ = "staff_hours"
    __table_args__ = (UniqueConstraint("staff_id", "weekday"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    staff_id: Mapped[int] = mapped_column(
        ForeignKey("staff.id", ondelete="CASCADE"), nullable=False
    )
    weekday: Mapped[str] = mapped_column(String, nullable=False)
    start: Mapped[time] = mapped_column(Time, nullable=False)
    end: Mapped[time] = mapped_column(Time, nullable=False)

    staff: Mapped[Staff] = relationship(Staff, back_populates="hours")


class Client(Base):
    """A person we've talked to before.

    ``phone`` is the normalized form (see ``salon.normalize_phone``): US
    numbers reduce to their last 10 digits. Uniqueness is enforced at the
    DB level so we can't accidentally create two rows for the same
    caller.

    ``customer_name`` on old appointments continues to hold whatever the
    LLM captured. Once ``client_id`` is populated the client's first/last
    name is the source of truth; the appointment's ``customer_name`` is
    just a historical snapshot of what was said on the call.
    """

    __tablename__ = "clients"
    __table_args__ = (Index("ix_clients_phone", "phone", unique=True),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    first_name: Mapped[str] = mapped_column(String, nullable=False, default="")
    last_name: Mapped[str] = mapped_column(String, nullable=False, default="")
    phone: Mapped[str] = mapped_column(String, nullable=False)
    email: Mapped[str | None] = mapped_column(String, nullable=True)
    gender: Mapped[str | None] = mapped_column(String, nullable=True)
    notes: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    appointments: Mapped[list["Appointment"]] = relationship(
        "Appointment",
        back_populates="client",
        # SET NULL preserves history if a client row is ever deleted —
        # the appointment survives with customer_name / customer_phone
        # intact but detached from any client record.
        passive_deletes=True,
    )


class Appointment(Base):
    """A booked appointment.

    ``customer_name`` and ``customer_phone`` are stored inline as a
    historical snapshot of what the caller (or admin) actually said.
    ``client_id`` links to the canonical Client row when we recognized
    the caller by phone number, or when an admin manually attached one.
    """

    __tablename__ = "appointments"

    id: Mapped[str] = mapped_column(String(16), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    customer_name: Mapped[str] = mapped_column(String, nullable=False)
    customer_phone: Mapped[str] = mapped_column(String, nullable=False)
    staff_id: Mapped[int] = mapped_column(
        ForeignKey("staff.id", ondelete="RESTRICT"), nullable=False
    )
    service_id: Mapped[int] = mapped_column(
        ForeignKey("services.id", ondelete="RESTRICT"), nullable=False
    )
    client_id: Mapped[int | None] = mapped_column(
        ForeignKey("clients.id", ondelete="SET NULL"), nullable=True, index=True,
    )
    date: Mapped[date] = mapped_column(Date, nullable=False)
    start_time: Mapped[time] = mapped_column(Time, nullable=False)
    end_time: Mapped[time] = mapped_column(Time, nullable=False)
    session_id: Mapped[str | None] = mapped_column(String, nullable=True)

    staff: Mapped[Staff] = relationship(Staff, lazy="joined")
    service: Mapped[Service] = relationship(Service, lazy="joined")
    client: Mapped[Client | None] = relationship(
        Client, back_populates="appointments", lazy="joined",
    )
