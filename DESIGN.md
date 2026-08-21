# Funkle Receptionist — Project Design Document

This document describes the system as implemented in this repository: an AI
phone receptionist for a hair salon. The live product is named **Funkle**.
Production runs at `https://salon-poc.fly.dev`.

The README covers how to run, deploy, and operate the app. This document
covers *why* the system is shaped the way it is: architecture, data model,
call flow, and the constraints that follow from those choices.

---

## 1. Purpose

Funkle answers a real phone number, talks with the caller in spoken English,
looks up salon hours/staff/services, checks availability, and books
appointments. A salon manager uses a web admin console to edit the same data
the bot uses, review call transcripts, and manage clients.

The product is a **single-salon, single-machine** system. It is built to feel
like a normal phone conversation, not to scale to many salons.

### In scope

- Inbound voice calls via Twilio
- Browser WebRTC test client (no phone required)
- Availability lookup and appointment booking
- Returning-caller recognition by phone number
- Admin CRUD for staff, services, hours, clients, appointments
- Call transcript observability
- Outbound SMS reminder ~24 hours before an appointment

### Out of scope (current code)

- Cancel / reschedule via voice (no LLM tools for this)
- Inbound SMS / STOP handling beyond the reminder copy
- Multi-salon / multi-tenant
- Authentication beyond HTTP Basic Auth on `/admin` and `/api`
- High availability, horizontal scaling, or a separate worker process

---

## 2. High-level architecture

One FastAPI process (`server.py`) is the whole runtime: HTTP, WebSockets, the
voice pipeline, the admin API, static/admin UI serving, and an in-process
reminder poller.

```
Caller phone                    Browser tester              Salon manager
     │                               │                            │
     ▼ PSTN                          ▼ WebRTC                     ▼ HTTPS
  Twilio ──POST /voice──► FastAPI (server.py) ◄── /admin + /api ──┘
     │                         │
     └── WS /twilio/ws ──► bot.py (Pipecat pipeline)
                                │
                                ├── Deepgram STT (cloud)
                                ├── OpenAI LLM  (cloud)
                                ├── Piper TTS   (local CPU)
                                └── salon.py tools → SQLite
```

**Transport is swappable.** `bot.py` takes a Pipecat `BaseTransport`.
`server.py` supplies either:

| Path | Transport | Sample rate | Caller identity |
|---|---|---|---|
| Twilio Media Stream `/twilio/ws` | `FastAPIWebsocketTransport` + `TwilioFrameSerializer` | 8 kHz μ-law | Twilio `From` |
| Browser `/offer` | `SmallWebRTCTransport` | 16 kHz | none |

The same pipeline, tools, and salon data serve both.

---

## 3. Call flow

### 3.1 Inbound phone call

1. Caller dials the Twilio number.
2. Twilio `POST`s `/voice`. The handler reads form field `From` (caller ID)
   and returns TwiML:

```xml
<Connect>
  <Stream url="wss://<public-host>/twilio/ws">
    <Parameter name="from" value="+1…"/>
  </Stream>
</Connect>
```

The WebSocket host comes from `X-Forwarded-Host` when present (ngrok / Fly
proxy).

3. Twilio opens `/twilio/ws`. The server waits for the Media Stream `start`
   event, extracts `streamSid`, `callSid`, and `customParameters.from`, then
   builds the Twilio serializer + websocket transport.
4. `run_bot(...)` starts. A new 8-char `session_id` is generated for logging
   and for stamping bookings.
5. Audio loops through VAD → STT → LLM → TTS until hangup.
   `on_client_disconnected` cancels the pipeline.

### 3.2 Browser test

`GET /` serves `static/index.html`. The page creates an `RTCPeerConnection`,
`POST`s an SDP offer to `/offer`, and patches ICE via `PATCH /offer`. No
phone number is available, so the system prompt says so explicitly.

---

## 4. Voice pipeline

Defined in `bot.py`. One pipeline instance per call.

```
transport.input
  → Silero VAD
  → Deepgram STT
  → TranscriptLogger (user)
  → LLM user aggregator
  → OpenAI LLM  (+ tools)
  → TranscriptLogger (assistant)
  → Piper TTS
  → transport.output
  → LLM assistant aggregator
```

### 4.1 Voice activity detection

Silero VAD (ONNX, CPU) decides turn boundaries:

| Param | Value | Intent |
|---|---|---|
| `confidence` | 0.7 | Reject uncertain speech |
| `start_secs` | 0.2 | Start of utterance |
| `stop_secs` | 1.2 | Allow mid-sentence pauses on phone |
| `min_volume` | 0.5 | Ignore very quiet noise |

Deepgram `interim_results` is off; VAD, not the STT service, owns
end-of-turn.

### 4.2 Speech-to-text

- Provider: Deepgram (`DEEPGRAM_MODEL`, default `nova-3-general`)
- English pinned; `smart_format` and `punctuate` on so spoken times become
  parseable text (`"ten thirty"` → `"10:30"`)
- Chosen so the app needs **no GPU**. Local Whisper was an earlier path and
  is not in the current runtime.

### 4.3 Language model

- Default: `gpt-4o-mini` (`OPENAI_MODEL`)
- System prompt is rebuilt **per call** with:
  - today’s date and weekday
  - spoken-style rules (short replies, no markdown, natural times)
  - `salon.caller_context(phone)` (who is on the line)
  - `salon.system_prompt_context()` (hours, staff, services, schedules,
    durations)

The bot waits for the caller to speak first. It is instructed never to
invent availability; it must call `check_availability` before claiming a
slot is free or taken.

### 4.4 Tools

Two functions, registered on the LLM and closed over `session_id` +
normalized caller phone so the model does not have to pass bookkeeping IDs:

**`check_availability(date_iso, stylist?, service?)`**

- Closed days return `{closed: true}`.
- Filters stylists by name and by who offers the service.
- Slot length comes from the service duration (default 30 minutes).
- Available starts are 30-minute grid points that fit in the intersection
  of salon hours and that stylist’s schedule, with no overlap against that
  day’s bookings.

**`book_appointment(...)`**

- Re-validates stylist, service, hours, and schedule.
- Conflict-checks **inside the same DB transaction** as the insert.
- Upserts a `Client` from the Twilio caller number (preferred) or the
  LLM-captured phone.
- Stores the LLM-captured `customer_phone` on the appointment as a
  historical snapshot.
- Stamps `session_id` so the Calls UI can link transcript → booking.

The tool schema currently enums services as `cut | color | perm | shave`.
The database can hold other service names via the admin UI; the voice path
is narrower than the data model.

### 4.5 Text-to-speech

Piper, local CPU, default voice `en_US-amy-medium`. The Docker image
pre-downloads the voice so the first production call is not blocked on a
~63 MB fetch.

---

## 5. Domain and data design

### 5.1 Persistence

SQLAlchemy 2.x, sync sessions, SQLite by default.

Connection resolution (`db.py`):

1. `DATABASE_URL` if set (Postgres-ready)
2. else `SALON_DB_PATH`
3. else `./receptionist.db`

SQLite pragmas: `foreign_keys=ON`, `journal_mode=WAL`,
`synchronous=NORMAL`. WAL is intentional so a voice booking and an admin
edit can overlap without readers blocking writers.

Schema changes go through Alembic. `entrypoint.sh` runs
`alembic upgrade head` on every container boot.

### 5.2 Schema

```
Salon (id=1) ── location string
Hours        ── one row per weekday; open/close NULL = closed
Service      ── name, duration_minutes, price
Staff        ── name
StaffService ── M2M staff ↔ services (CASCADE)
StaffHours   ── staff × weekday unique; start/end
Client       ── unique normalized phone
Appointment ── staff RESTRICT, service RESTRICT, client SET NULL
```

Design rules encoded in `models.py`:

- Appointment IDs are 8-char hex strings (legacy workbook shape).
- Staff/service are FKs, not names, so a rename does not rewrite history.
- Deleting staff or a service that still has appointments **fails**
  (`ON DELETE RESTRICT`). The xlsx era silently orphaned rows.
- Deleting a client **detaches** appointments (`ON DELETE SET NULL`);
  `customer_name` / `customer_phone` remain as snapshots.
- Client phone is the last 10 US digits. Lookups and uniqueness use that
  form.

Migrations in order:

1. `2b8b9190513e` — initial salon schema
2. `89b29a9a65a1` — `clients` + `appointments.client_id`
3. `56189eb5c075` — reminder status columns

### 5.3 In-memory cache

`salon.INFO` (`SalonInfo`) caches location, hours, stylists, and services
at import and after every admin mutation via `reload()`. The voice bot
answers static questions from this cache so it does not hit SQLite on every
LLM turn. Appointment rows are always read/written live.

Writes are serialized with:

- `asyncio.Lock` around the two bot tools (so two concurrent calls cannot
  double-book)
- `threading.RLock` around the validate → conflict-check → insert sequence

Admin CRUD uses the same lock and then `reload()`.

### 5.4 Slot math

- Grid: 30 minutes (`SLOT_MIN`)
- Service duration must be a positive multiple of 30
- A slot is free if `[start, start+duration)` is inside both salon hours
  and the stylist’s work window and does not overlap any existing
  appointment for that stylist on that date

Admin calendar booking uses the same overlap helper
(`_admin_conflict_query`).

### 5.5 Caller identity

On a phone call:

1. Twilio `From` is forwarded into the Media Stream.
2. `bot.py` normalizes it and injects `CALLER INFO` into the prompt (name,
   notes, upcoming appointments, last three past visits).
3. The prompt forbids asking for a callback number; the Twilio number is
   the booking phone unless the caller asks otherwise.
4. On book, Twilio’s number wins for the `Client` link even if STT mangled
   the spoken digits.

Browser tests have no caller ID; the prompt says so.

Excel (`ReceptionistData.xlsx`) is **legacy**. The running server does not
read it. `import_xlsx` / `export_xlsx` exist for first-boot seed, volume
migration, and spreadsheet backup. `entrypoint.sh` imports once on an empty
DB, then renames the workbook to `*.imported`.

---

## 6. Admin console

Angular 22 standalone SPA in `admin-ui/`, Material UI, lazy-loaded routes.
Production build is copied into the Python image and served at `/admin/` by
FastAPI (deep links fall through to `index.html`).

| Page | Role |
|---|---|
| Dashboard | Counts: staff, services, appointments, clients, calls; salon location |
| Staff | Name, offered services, weekly schedule |
| Services | Name, duration, price |
| Clients | Search, CRUD, appointment history; delete detaches history |
| Calendar | Week grid; create/edit/cancel; conflict detection; client badge; reminder status |
| Calls | Sessions from `transcripts.log`; duration, turns, booked vs not; full transcript |

`ApiService` uses `/api` when served from FastAPI, and
`http://127.0.0.1:7860/api` when `ng serve` is on port 4200. CORS in
`server.py` allows those localhost origins.

REST surface (`admin_api.py`, prefix `/api`):

- `GET /summary`
- Staff / services / hours / location CRUD
- Appointments with optional `start`/`end` query
- Clients including `GET /clients/by-phone/{phone}` (404 → UI treats as new)
- `GET /calls`, `GET /calls/{session_id}`

Blocking salon/DB work is dispatched with `asyncio.to_thread`. Validation
errors → 400; missing rows → 404.

---

## 7. Authentication

`AdminBasicAuthMiddleware` (`auth.py`):

- Protects only `/admin` and `/api`
- Voice (`/voice`, `/twilio/ws`), browser client (`/`, `/offer`), and
  static assets stay public
- Enabled only when `ADMIN_PASSWORD` is set
- Constant-time compare (`secrets.compare_digest`)
- CORS preflight (`OPTIONS`) is allowed through without credentials

Production must set `ADMIN_USERNAME` / `ADMIN_PASSWORD` as Fly secrets.
Locally, unset password keeps the admin UI open for development.

---

## 8. SMS reminders

`reminders.py` + a background task in `server.py`.

- Poll interval: `REMINDER_POLL_SECONDS` (default 300)
- First tick delayed 15 seconds so boot migrations finish
- Tick failures are logged; they do not kill the voice server

An appointment is due when its **salon-local** start (`SALON_TZ`, default
`America/New_York`) is 23–25 hours from now and `reminder_status` is
`pending` or `failed`.

Statuses: `pending` → `sent` | `failed` | `skipped`.

- Phone: prefer linked client, else appointment snapshot; must normalize
  to E.164 `+1…`
- Unusable phone → `skipped`
- Failed send is retried on later ticks **while still inside the window**;
  after the 23h floor, retries stop
- From number: `TWILIO_FROM_NUMBER` (same E.164 as the voice number)
- Copy includes “Reply STOP to opt out”; **no inbound SMS handler is
  implemented**

---

## 9. Observability

`log_config.py` owns `LOG_DIR` (local `./logs`, Fly `/data/logs`).

| Sink | Content | Rotation |
|---|---|---|
| stderr | Non-transcript logs | — |
| `server.log` | Full server + intercepted uvicorn/pipecat | 10 MB / 14 days |
| `transcripts.log` | One line per conversational turn | 5 MB / 30 days |

Transcript format is a contract with `calls.py`:

```
YYYY-MM-DD HH:MM:SS [sessionid]      user: text
YYYY-MM-DD HH:MM:SS [sessionid] assistant: text
```

`calls.py` parses live + rotated files, groups by session, computes
duration/turns, and joins appointments on `session_id`. Outcome is `booked`
if any appointment exists for that session, else `no_booking`.

Pipecat metrics (`enable_metrics=True`) log TTFB for Deepgram and OpenAI,
which is the primary production health signal for the pipeline.

---

## 10. Deployment and operations

### Runtime

- Python 3.11, FastAPI + uvicorn
- Docker: Node 22 builds Angular; `python:3.11-slim` runs the app
- Fly.io app `salon-poc`, region `iad`, `shared-cpu-1x` / 2 GB RAM
- Persistent volume `receptionist_data` → `/data` (SQLite + logs)
- `auto_stop_machines = "off"`, `min_machines_running = 1` so a Twilio
  WebSocket is never dropped by idle stop
- HTTP concurrency: soft 20 / hard 40 connections (LLM and Deepgram are
  the real limits)

Boot (`entrypoint.sh`): migrate → if DB empty, import volume xlsx or image
seed → `exec python server.py`.

### Config vs secrets

| Public (`fly.toml` `[env]`) | Secrets |
|---|---|
| `HOST`, `PORT`, `SALON_DB_PATH`, `SALON_TZ`, models, `LOG_DIR`, `REMINDER_POLL_SECONDS` | `OPENAI_API_KEY`, `DEEPGRAM_API_KEY`, Twilio SID/token/from, `ADMIN_USERNAME`, `ADMIN_PASSWORD` |

Fly secrets override `[env]`. Putting `PORT` in both has previously caused
hung HTTP.

### CI/CD

- `ci.yml`: Python `compileall` + Angular production build on PRs /
  non-main pushes
- `deploy.yml`: merge to `main` → `flyctl deploy --remote-only`
  (repo-gated to `LoganDiliberto/Receptionist`)

### Local phone testing

`run.py` starts `server.py` and `ngrok http 7860`, prints `https://…/voice`
for the Twilio webhook. Production webhook should be restored to the Fly
URL after local tests.

---

## 11. Cost and vendor choices

Approximate per 5-minute call: a few cents (LLM + Deepgram + Twilio), plus
~$5–10/month Fly.

| Concern | Choice | Trade-off |
|---|---|---|
| STT | Deepgram cloud | No GPU, better phone accuracy; per-minute cost |
| TTS | Piper local | Free; less natural than hosted TTS |
| PSTN | Twilio Media Streams | No SIP ops; per-minute + number fee |
| Data | SQLite on one volume | Zero-ops for one salon; not multi-machine |
| LLM | gpt-4o-mini | Cheap enough for spoken short turns; can bump to gpt-4o if quality is poor |

---

## 12. Constraints and known risks

- **One process, one salon.** Reminder polling, voice, and admin share the
  same event loop and SQLite file.
- **US-centric phones.** Normalization keeps last 10 digits and SMS uses
  `+1`.
- **Voice service enum is hardcoded** while the DB/admin services are
  free-form.
- **No voice cancel/reschedule.** Returning callers get upcoming
  appointments in the prompt, but there is no tool to change them.
- **Admin appointments do not auto-create clients** the way the voice book
  path does (`create_appointment` does not upsert `Client`).
- **Call history is a log file**, not a table. Rotation/retention will
  eventually drop old transcripts; bookings remain in SQLite.
- **Basic Auth** is enough for a private salon console, not for multi-user
  RBAC.
- **Concurrency model** is “one machine, WAL + locks.” Two Fly machines on
  one SQLite file would be unsafe.

---

## 13. Module map

| Module | Responsibility |
|---|---|
| `server.py` | HTTP/WS, transports, reminder loop, admin SPA serving |
| `bot.py` | Pipecat pipeline, prompt, tools, session IDs |
| `salon.py` | Domain logic, cache, availability/booking, CRUD |
| `models.py` / `db.py` | ORM + engine |
| `admin_api.py` | REST for the Angular app |
| `auth.py` | Basic Auth gate |
| `reminders.py` | 24h SMS |
| `calls.py` | Transcript → call records |
| `log_config.py` | Loguru sinks |
| `run.py` | Dev: server + ngrok |
| `import_xlsx.py` / `export_xlsx.py` / `backfill_clients.py` | Data migration CLIs |
| `admin-ui/` | Angular manager console |
| `static/index.html` | WebRTC test phone |

---

## 14. Evolution notes (from the current design, not implemented)

The code already leaves doors open:

- Swap Twilio for another PSTN provider by changing only `server.py`
  transport wiring.
- Point `DATABASE_URL` at Postgres when SQLite is no longer enough.
- Restore local Whisper STT by swapping `DeepgramSTTService` (GPU / extra
  deps required).
- Appointment IDs can become integer PKs later; 8-char hex is
  compatibility, not a long-term constraint.

Natural next product slices, given what is missing: voice
cancel/reschedule, linking admin-created appointments to clients, inbound
SMS STOP, and storing call sessions in the DB instead of only in logs.
