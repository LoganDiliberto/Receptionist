"""Call observability: turn the transcript log into structured call records.

The bot writes one line per turn to `logs/transcripts.log` in a fixed loguru
format defined in `server.py`:

    2026-06-29 10:36:43 [4ce16399]      user:  Welcome
    2026-06-29 10:36:46 [4ce16399] assistant: What can I help you with today?

We parse every rotated log file plus the live one, group lines by session id,
compute a rough start/end/duration, and try to link each call to appointments
that reference the same `session_id` in the workbook.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import salon

LOG_DIR = Path(__file__).parent / "logs"
TRANSCRIPT_GLOB = "transcripts.log*"

# Loguru pads role to 9 chars right-aligned, which means "user" comes through
# as "     user" and "assistant" as-is. The (?P<role>\w+) group happily eats
# either case once we've consumed the surrounding whitespace.
_LINE_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) "
    r"\[(?P<session>[0-9a-f]{8})\]\s+"
    r"(?P<role>\w+):\s?(?P<text>.*)$"
)


@dataclass
class Turn:
    at: datetime
    role: str  # "user" | "assistant"
    text: str


@dataclass
class Call:
    session_id: str
    started_at: datetime
    ended_at: datetime
    turns: list[Turn]

    @property
    def duration_seconds(self) -> int:
        return int((self.ended_at - self.started_at).total_seconds())

    @property
    def turn_count(self) -> int:
        return len(self.turns)

    @property
    def user_turn_count(self) -> int:
        return sum(1 for t in self.turns if t.role == "user")


def _iter_lines() -> list[str]:
    """Read every transcript log file (rotated + live), oldest first."""
    if not LOG_DIR.exists():
        return []
    files = sorted(LOG_DIR.glob(TRANSCRIPT_GLOB))
    lines: list[str] = []
    for f in files:
        try:
            lines.extend(f.read_text(encoding="utf-8", errors="replace").splitlines())
        except OSError:
            continue
    return lines


def _parse_calls() -> dict[str, Call]:
    by_session: dict[str, Call] = {}
    for line in _iter_lines():
        m = _LINE_RE.match(line)
        if not m:
            continue
        try:
            ts = datetime.strptime(m.group("ts"), "%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
        session = m.group("session")
        role = m.group("role").strip().lower()
        text = m.group("text").strip()
        turn = Turn(at=ts, role=role, text=text)
        call = by_session.get(session)
        if call is None:
            by_session[session] = Call(
                session_id=session, started_at=ts, ended_at=ts, turns=[turn]
            )
        else:
            call.turns.append(turn)
            if ts < call.started_at:
                call.started_at = ts
            if ts > call.ended_at:
                call.ended_at = ts
    return by_session


def _appointments_by_session() -> dict[str, list[dict]]:
    """Group persisted appointments by the session_id the bot booked them under."""
    out: dict[str, list[dict]] = {}
    for appt in salon.list_appointments():
        sid = appt.get("session_id")
        if not sid:
            continue
        out.setdefault(str(sid), []).append(appt)
    return out


def _summary_for(call: Call, appts: list[dict]) -> dict:
    """A one-line JSON-friendly summary of a call for the list view."""
    outcome = "booked" if appts else "no_booking"
    return {
        "session_id": call.session_id,
        "started_at": call.started_at.isoformat(timespec="seconds"),
        "ended_at": call.ended_at.isoformat(timespec="seconds"),
        "duration_seconds": call.duration_seconds,
        "turn_count": call.turn_count,
        "user_turn_count": call.user_turn_count,
        "outcome": outcome,
        "appointment_ids": [a["id"] for a in appts],
    }


def list_calls() -> list[dict]:
    """All known calls, newest first, with a compact summary and outcome."""
    calls = _parse_calls()
    appts_by_session = _appointments_by_session()
    out = [
        _summary_for(call, appts_by_session.get(call.session_id, []))
        for call in calls.values()
    ]
    out.sort(key=lambda c: c["started_at"], reverse=True)
    return out


def get_call(session_id: str) -> dict | None:
    """Full detail for one call: transcript turns + linked appointments."""
    calls = _parse_calls()
    call = calls.get(session_id)
    if call is None:
        return None
    appts = _appointments_by_session().get(session_id, [])
    summary = _summary_for(call, appts)
    summary["turns"] = [
        {"at": t.at.isoformat(timespec="seconds"), "role": t.role, "text": t.text}
        for t in sorted(call.turns, key=lambda t: t.at)
    ]
    summary["appointments"] = appts
    return summary
