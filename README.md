# Receptionist Project
An AI receptionist that answers a phone. Someone calls a real phone number, the
AI picks up, has a real conversation with them, takes a message, and hangs up.

Architecture, data model, call flow, and design trade-offs are in
[DESIGN.md](DESIGN.md). This README covers how to run, deploy, and operate
the app.

---

## What it does, in one paragraph

A caller dials a Twilio phone number. Twilio sends the live call audio over the
internet to this program. The program listens to the audio with a speech-to-text
model, decides what to say back using an AI language model, speaks the reply with
a text-to-speech voice, and sends that voice audio back to Twilio so the caller
hears it. All of this happens fast enough to feel like a normal conversation.

---

## How it works (the picture)

```
   Caller's phone
        │
        ▼  (phone network)
     Twilio          ←  Twilio answers the call and bridges
        │                 the audio to us over the internet.
        ▼  (WebSocket — a live data pipe)
   server.py         ←  Our Python program. Receives audio chunks,
        │                 sends audio chunks back.
        ▼
   ┌─────────────────────────────────────────────────┐
   │  The voice pipeline (bot.py)                    │
   │                                                  │
   │  1. Voice Activity Detection                     │
   │     "Is the caller speaking right now?"          │
   │                                                  │
   │  2. Speech-to-Text  (Deepgram nova-3, cloud)     │
   │     Caller's audio → written words               │
   │                                                  │
   │  3. Language Model  (OpenAI gpt-4o-mini)         │
   │     Written words → written reply                │
   │                                                  │
   │  4. Text-to-Speech  (Piper, local CPU)           │
   │     Written reply → spoken audio                 │
   └─────────────────────────────────────────────────┘
        │
        ▲  (audio reply travels back the same way)
        │
   Caller hears the reply
```

The whole loop runs continuously and concurrently. While the AI is replying,
the program is already listening for the caller to start talking again so it
can stop talking and listen.

---

## What happens during a call, step by step

1. Caller dials your Twilio number.
2. Twilio answers and asks our server "what should I do with this call?"
   by hitting `POST /voice`.
3. Our server replies with a tiny instruction (called *TwiML*) that says:
   "Connect the live audio of this call to my WebSocket at `/twilio/ws`."
4. Twilio opens that WebSocket. From now on, every ~20 milliseconds of caller
   audio is sent over the WebSocket as a small chunk of bytes.
5. The program runs each chunk through the pipeline above. Deepgram
   streams transcriptions back in real time; when the VAD says the caller
   has stopped talking, the finalized text is handed to the AI.
6. The AI's reply is streamed token-by-token into Piper, which produces audio
   bytes that are sent back over the same WebSocket.
7. Twilio plays those bytes to the caller, who hears the receptionist speak.
8. When the caller hangs up, the WebSocket closes and the pipeline shuts down.

A browser test client (`static/index.html`) does the same thing via WebRTC
instead of a phone, so you can test changes without making a phone call.

---

## The pieces (what each file does)

| File | Job |
|---|---|
| `server.py` | The web server. Accepts incoming calls (from the browser or from Twilio), sets up a transport for each call, and starts a bot to run the conversation. Also serves the admin UI and its REST API. |
| `auth.py` | HTTP Basic Auth middleware that gates `/admin` and `/api` when `ADMIN_PASSWORD` is set. Voice (`/voice`, `/twilio/ws`) and the browser test client stay public. |
| `log_config.py` | Shared Loguru setup: `LOG_DIR`, rotation/retention, and stdlib interception so uvicorn/pipecat land in the same files. |
| `run.py` | Convenience launcher: spawns `server.py` and `ngrok http 7860` together and prints the public URL you paste into Twilio. Also `run.cmd` for a shorter double-click / one-word invocation on Windows. |
| `bot.py` | Defines the voice pipeline — the chain of components that turn caller audio into a reply. Transport-agnostic, so the same bot works for browser tests and real phone calls. When a caller's phone number is known, `bot.py` injects their name and appointment history into the system prompt so the LLM greets returning callers by name. |
| `salon.py` | The salon data layer. Reads and writes the SQLite database (hours, staff, services, schedules, appointments, **clients**) via SQLAlchemy and exposes the async tools (`check_availability`, `book_appointment`) the LLM calls, plus CRUD helpers used by the admin API. Includes `normalize_phone` / `format_phone` and the `caller_context()` block that gets injected into the bot's system prompt on each call. |
| `models.py` | SQLAlchemy ORM models: `Salon`, `Hours`, `Service`, `Staff`, `StaffHours`, `Appointment`, and `Client`. Appointments carry a nullable `client_id` FK to `Client`. |
| `db.py` | SQLAlchemy engine + `session_scope` context manager. Reads the DB URL from `DATABASE_URL` (Postgres, if you ever add it) or falls back to a SQLite file at `SALON_DB_PATH`. |
| `alembic/` | Schema migrations. `alembic upgrade head` runs automatically on every container boot (see `entrypoint.sh`). |
| `import_xlsx.py` / `export_xlsx.py` | One-shot CLIs to move data between the legacy `ReceptionistData.xlsx` and the SQLite database. Only used for onboarding a new salon or debugging — the running server touches only the DB. |
| `backfill_clients.py` | Post-Phase-2 one-shot: creates `Client` rows for each unique `Appointment.customer_phone` in the DB and links appointments to them. Idempotent — safe to re-run. Dry-run by default; pass `--commit` to write. |
| `reminders.py` | Outbound appointment SMS (~24h before start) via Twilio. An in-process poller in `server.py` calls `run_reminder_tick` every few minutes. Status is stored on each appointment (`pending` / `sent` / `failed` / `skipped`). |
| `calls.py` | Parses `transcripts.log` (under `LOG_DIR`) into structured call records and links each call to the appointment it produced (via the `session_id` column). |
| `admin_api.py` | FastAPI router mounted at `/api` that exposes staff/services/hours/appointments/**clients**/calls to the admin UI. |
| `admin-ui/` | Angular admin console (built with `npm run build`; served by FastAPI at `/admin`). |
| `static/index.html` | A small webpage that lets you "call" the bot from your browser for testing. |
| `voices/` | The Piper text-to-speech voice files. Not committed (see `.gitignore`) — Piper auto-downloads the voice named by `PIPER_VOICE` on first synthesis, and the Docker build pre-bakes it so the first call has no download latency. |
| `logs/server.log` | Everything the program logs, rotated. Local default under `./logs`; on Fly this is `/data/logs/server.log`. |
| `logs/transcripts.log` | Just the conversation transcripts. One line per turn. Same `LOG_DIR` as above — survives redeploys on Fly. |
| `.env` | Secrets and settings: your OpenAI API key, Deepgram API key, Twilio credentials, model choices. Not committed to git. |
| `.env.example` | A template showing what `.env` should contain. |
| `Dockerfile` / `.dockerignore` | How production builds the container image (multi-stage: Angular build + Python runtime). |
| `entrypoint.sh` | Container startup shim. Copies the seed workbook onto the persistent volume on first boot, then execs the server. |
| `fly.toml` | Fly.io deployment config: region, machine size, port, and the persistent volume mount. |
| `requirements.txt` | Python runtime dependencies pinned for reproducible deploys. |
| `.github/workflows/deploy.yml` | CI/CD: merges to `main` deploy to Fly.io automatically. |
| `.github/workflows/ci.yml` | Runs on every push and PR: Python syntax check + Angular build. |

---

## What you need to run it

- Python 3.11 (any OS — Windows, macOS, Linux, or a Docker container).
- An OpenAI API key.
- A Deepgram API key (free $200 of credit at signup — plenty for testing).
- A Twilio account with a phone number (for real calls — browser testing
  doesn't need this).
- `ngrok` (so Twilio can reach your laptop over the public internet during
  development). Not needed in production — Fly.io gives us a stable URL.

> Speech-to-text used to run locally on an NVIDIA GPU via faster-whisper.
> The current pipeline uses Deepgram for STT, which means the app has no
> GPU requirement and can be deployed to any commodity container host.
> If you want to run STT locally again, revert `bot.py` to use
> `WhisperSTTService` and re-install `pipecat-ai[whisper]`.

---

## Running it

### Browser test (no phone needed)

1. From the project folder, start the server:
   ```
   .venv\Scripts\python.exe server.py
   ```
2. Open http://127.0.0.1:7860 in your browser.
3. Click **Start call**, allow microphone access, and talk.

### Real phone call (development only)

In production the Twilio webhook points at the stable Fly.io URL — see
[Deploying to production](#deploying-to-production-flyio). This section
is for when you want to test a phone call against **local** code changes
before shipping them, without pushing a full deploy.

The easy way — one command starts both the server and ngrok, prints the
public URL you need for Twilio, and shuts everything down on Ctrl+C:

```
run
```

(or `.venv\Scripts\python.exe run.py` if you'd rather not use the `.cmd`
shortcut.) Copy the printed `Twilio webhook` URL into your Twilio number's
"A call comes in" webhook (POST) in the Twilio Console, dial the number,
then **switch the webhook back to the Fly URL** when you're done so
production traffic isn't going to your laptop. See `run.py` — it wraps
`server.py` and `ngrok http 7860` so you don't have to babysit two
terminals.

If you'd rather run them yourself:

1. Start the server: `.venv\Scripts\python.exe server.py`
2. In a second terminal: `ngrok http 7860`
3. Copy the `https://*.ngrok-free.app` URL ngrok shows.
4. In the Twilio Console, open your number's settings. For "A call comes in",
   set the webhook to `https://<your-ngrok-url>/voice` (POST).
5. Dial your Twilio number from any phone.
6. When you're done, put the Fly URL back in the Twilio webhook.

---

## Admin console (Angular)

The `admin-ui/` folder holds a small single-page Angular app that lets a
manager edit staff, services, hours, and the appointment calendar directly
without touching Excel. It talks to the FastAPI server over `/api/*`, which
persists changes back into the same `ReceptionistData.xlsx` workbook the bot
reads on startup — so an edit made in the UI is picked up by the next call.

### First-time setup

```
cd admin-ui
npm install
npm run build
```

The build writes to `admin-ui/dist/admin-ui/browser/`. When the FastAPI
server sees that folder exists, it serves the compiled app at
`http://<host>:7860/admin/` and returns any deep link (e.g. `/admin/staff`)
back to `index.html` so the Angular router can handle it.

### Day-to-day development

Run the API and the Angular dev server in two terminals:

```
# terminal 1 — API + voice bot
.venv\Scripts\python.exe server.py

# terminal 2 — Angular dev server with live reload
cd admin-ui
npm start
```

Then open `http://127.0.0.1:4200/`. The dev server proxies API calls to the
FastAPI server on port 7860; CORS for `localhost:4200` is already configured
in `server.py`.

If you set `ADMIN_PASSWORD` locally, the Angular dev server on `:4200` will
get `401`s from `/api` (browsers don't auto-attach Basic Auth across
origins). Easiest options: leave `ADMIN_PASSWORD` unset while using
`ng serve`, or open the built app at `http://127.0.0.1:7860/admin` instead
(same origin — the browser login dialog works).

### What the pages do

- **Dashboard** — quick counts of staff, services, appointments, clients,
  and calls, plus the salon's location.
- **Staff** — add, edit, or remove staff members; pick which services they
  offer and set their weekly schedule.
- **Services** — add, edit, or remove services and their duration and price.
- **Clients** — the salon's address book. Search by name or phone;
  create/edit/delete clients; view each client's upcoming and past
  appointments in one place. Clients are populated automatically the
  first time someone books over the phone — the bot upserts the caller
  into this table using Twilio's `From` number as the primary key, so
  the second call greets them by name. You can also add walk-ins
  manually. Deleting a client detaches (but preserves) their historical
  appointments.
- **Calendar** — a week-at-a-glance grid of upcoming appointments. Click any
  day to add a new booking, or click an existing appointment to edit or
  cancel it. Uses the same conflict-detection logic the voice bot does.
  When an appointment is linked to a client, the editor shows a
  "Linked to client" badge below the phone field. Each appointment also
  shows its SMS reminder status (pending / sent / failed).
- **Calls** — an observability view of every call the bot has answered.
  Each row shows when the call happened, how long it lasted, how many turns
  it took, and whether it resulted in a booking. Click a row for the full
  transcript and a "view in calendar" link to any appointment the call
  produced. Data is derived from `transcripts.log` (see `LOG_DIR`) plus the
  `session_id` column the bot now stamps on every appointment it books.

---

## How to tune it when it goes wrong

Watch `logs/transcripts.log` locally (or `/data/logs/transcripts.log` on
Fly), or `fly logs -a salon-poc` for the live stream. You'll see what the
program actually heard you say and what it actually replied. In production,
the **Calls** page in the admin UI also gives you a per-call transcript
view. From there:

- **It heard the wrong words.** Try a phone-tuned Deepgram model —
  `DEEPGRAM_MODEL=nova-2-phonecall` is trained specifically on
  narrow-band phone audio. Set it in `fly.toml [env]` (not as a secret)
  and redeploy. If a specific accent trips it up, boost key vocabulary
  via the `keywords` setting on `DeepgramSTTService`.
- **It heard you correctly but said something dumb.** Use a smarter
  language model (`OPENAI_MODEL=gpt-4o` in `fly.toml [env]`).
- **It cut you off mid-sentence.** Increase `stop_secs` in `bot.py` (the
  VAD setting). Higher means more pause tolerance, lower means snappier
  replies.
- **It didn't hear you at all.** Lower `confidence` (currently `0.7`) or
  `min_volume` (currently `0.5`) in the `VADParams` inside `bot.py` to
  let quieter or less-confident audio segments through.

---

## How much it costs to run

- **OpenAI** (gpt-4o-mini): about a tenth of a cent per minute of conversation.
- **Twilio** (US local number, inbound): $1.15 per month plus $0.0085 per minute.
- **Speech-to-text** (Deepgram nova-3): $0.0043 per minute of caller audio.
- **Text-to-speech** (Piper, local): free. Uses your CPU.
- **ngrok** (development only): free tier is fine. Stable URLs cost $8/month.
- **Fly.io** (production hosting): ~$5–10/month for one always-on `shared-cpu-1x`
  machine with a 1 GB volume. Free for most side-project traffic within the
  Hobby plan.

A 5-minute call costs you roughly seven cents (LLM + STT + Twilio + a
negligible slice of Fly.io).

---

## Deploying to production (Fly.io)

The live app is at **https://salon-poc.fly.dev** — one always-on
`shared-cpu-1x` machine in `iad`, backed by a 1 GB persistent volume for
the salon workbook. Merges to `main` deploy automatically via the
`.github/workflows/deploy.yml` workflow. The rest of this section
documents the one-time setup and the day-to-day operations.

> If you're forking this repo for a different salon, pick a new app
> name (`salon-poc` is already taken by us) and update the `app = "..."`
> line in `fly.toml` to match. Everywhere `salon-poc` appears below,
> substitute your own name.

### 1. Install the Fly CLI

Once, on your dev machine:

```powershell
# Windows — run in PowerShell, NOT cmd.exe (iwr is a PowerShell alias).
iwr https://fly.io/install.ps1 -useb | iex

# Windows alternative — winget works from either shell:
winget install Fly.Flyctl

# macOS / Linux
curl -L https://fly.io/install.sh | sh
```

Close and reopen your terminal after install so `PATH` picks up
`fly.exe`. Then `fly auth login` and pick (or create) the org you want
to bill.

### 2. Create the app AND its persistent volume

Both commands must run before the first deploy — a machine that mounts
`/data` can't start if there's no volume for it to attach to. Fly will
error out with `New machine ... needs an unattached volume named
'receptionist_data'` if you skip the second command.

```
fly launch --no-deploy --copy-config --name salon-poc
fly volumes create receptionist_data --region iad --size 1
```

`fly launch --no-deploy` just registers the app on Fly's side (it
notices the existing `fly.toml` and reuses it); `fly volumes create`
provisions the 1 GB volume that gets mounted at `/data` per `fly.toml`.

### 3. Set production secrets

Fly-managed **secrets** are for API keys and passwords only — never
non-sensitive config. Everything else goes in the `[env]` block of
`fly.toml` where it's version-controlled and visible in diffs (see
[Fly config vs. Fly secrets](#fly-config-vs-fly-secrets) below).

```
fly secrets set \
  OPENAI_API_KEY=sk-... \
  DEEPGRAM_API_KEY=... \
  TWILIO_ACCOUNT_SID=AC... \
  TWILIO_AUTH_TOKEN=... \
  TWILIO_FROM_NUMBER=+1XXXXXXXXXX \
  ADMIN_USERNAME=admin \
  ADMIN_PASSWORD='pick-a-long-random-password'
```

`TWILIO_FROM_NUMBER` is the salon voice number in E.164 — the same one
callers dial. It's also the From: address on the ~24h appointment
reminder SMS.

`ADMIN_PASSWORD` enables HTTP Basic Auth on `/admin` and `/api`. Without
it those paths are publicly reachable — always set this in production.
The browser will prompt for username/password the first time you open
`https://salon-poc.fly.dev/admin`.
### 4. First manual deploy

Prove the container image builds and boots before hooking up CI:

```
fly deploy
```

When it prints `Machine ... reached its target running state`, browse to
`https://salon-poc.fly.dev/admin` and confirm the admin UI loads. If it
hangs, the most common cause is a mismatch between the port the app
listens on and `internal_port` in `fly.toml` — see the debugging
section below.

### 5. Point Twilio at the Fly URL

In the Twilio Console, edit your number's "A call comes in" webhook to
`https://salon-poc.fly.dev/voice` (POST). Dial it — you should hear the
bot. No more ngrok in production.

### 6. Turn on CI/CD

Deploy-on-merge lives at `.github/workflows/deploy.yml`. It runs
`flyctl deploy --remote-only` whenever `main` receives a push, using a
long-lived Fly API token stored as a GitHub Actions secret.

```
# Generate a deploy-only token bound to this app (1 year expiry)
fly tokens create deploy -a salon-poc --expiry 8760h
```

Copy the token (starts with `FlyV1 fm2_...`). Then in GitHub:

1. Go to **Settings → Secrets and variables → Actions**.
2. Click **New repository secret**.
3. Name: `FLY_API_TOKEN`. Value: the token you just copied.

From now on: open a PR from any feature branch, get it reviewed, merge
to `main`, watch **Actions** show a green deploy, refresh the app URL.
That's it.

### Fly config vs. Fly secrets

Fly has two ways to inject environment variables into your container:
the `[env]` block in `fly.toml` and `fly secrets`. **Secrets override
`[env]` values.** Use them correctly or you'll get very confusing
behavior:

| Kind | Where it goes | What belongs there |
|---|---|---|
| Public config | `[env]` in `fly.toml` | `HOST`, `PORT`, `SALON_DB_PATH`, `SALON_DATA_PATH`, `OPENAI_MODEL`, `DEEPGRAM_MODEL`, `PIPER_VOICE`, `LOG_DIR`, `LOG_LEVEL`, `SALON_TZ`, `REMINDER_POLL_SECONDS` — anything you'd be happy to see in a git diff. |
| Actual secrets | `fly secrets set ...` | `OPENAI_API_KEY`, `DEEPGRAM_API_KEY`, `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_FROM_NUMBER`, `ADMIN_USERNAME`, `ADMIN_PASSWORD` — anything that grants access if leaked. |

We hit this the hard way once: `PORT` got set as a secret with an old
value, which overrode the `PORT=8080` in `fly.toml`. The app listened
on the old port, Fly's edge proxied to the new one, HTTP hung forever.
If you ever see that shape of failure, check `fly secrets list` first —
if you spot a name that also appears in `fly.toml [env]`, unset it:

```
fly secrets unset PORT HOST DEEPGRAM_MODEL OPENAI_MODEL
```

### Debugging a call in production

Something went wrong on a phone call. In order, check:

1. **Did Twilio even reach us?**
   Twilio Console → **Monitor → Logs → Calls** shows every inbound
   call and its status. If it's not there, your phone never got to
   Twilio (dialed wrong number, no carrier signal, or the number
   isn't provisioned). If it's there with an error, click for the
   webhook error. If Twilio thinks it succeeded but you saw no bot,
   the webhook URL is probably pointing somewhere stale (e.g. old
   ngrok URL from dev testing).
2. **Did our server get the request?**
   `fly logs` should show `POST /voice` followed by a WebSocket
   accept on `/twilio/ws`. If `/voice` fires but the WS never
   accepts, the TwiML response is pointing at the wrong host. If
   neither shows up, Twilio's webhook still points somewhere else.
3. **Did the bot pipeline start and run?**
   Look for `DeepgramSTTService#0 TTFB: ...s` and
   `OpenAILLMService#0 TTFB: ...s` lines. Their absence means the
   pipeline crashed at startup — usually a missing or wrong API
   key. `Disconnecting from Deepgram` at the end of a session is
   the normal shutdown.
4. **Deepgram dashboard shows zero usage?**
   Almost always the wrong-project trap: Deepgram accounts have
   multiple projects, and usage rolls up per-project. Confirm the
   key you set as `DEEPGRAM_API_KEY` is listed under the project
   you're viewing in the console (**Settings → API Keys**). Also
   remember the dashboard has a 5–15 minute reporting lag.

### Day-to-day operations

| Task | Command |
|---|---|
| Tail production logs | `fly logs -a salon-poc` |
| Inspect persisted log files on the volume | `fly ssh console -a salon-poc -C "ls -la /data/logs"` |
| Open a shell in the running container | `fly ssh console -a salon-poc` |
| Roll back to the previous release | `fly releases -a salon-poc` then `fly deploy --image <previous-image-ref>` |
| Export a spreadsheet backup of the live DB | `fly ssh console -a salon-poc -C "python -m export_xlsx /data/backup.xlsx"` then `fly ssh sftp get /data/backup.xlsx` |
| Backfill clients from existing appointments (one-shot after Clients ships) | Dry run: `fly ssh console -a salon-poc -C "python -m backfill_clients"`. Write: `... -C "python -m backfill_clients --commit"` |
| Set the SMS From number (required for reminders) | `fly secrets set TWILIO_FROM_NUMBER=+1XXXXXXXXXX -a salon-poc` (same E.164 number used for voice) |
| Set admin login (required — locks /admin and /api) | `fly secrets set ADMIN_USERNAME=admin ADMIN_PASSWORD='...' -a salon-poc` |
| Manually run one reminder tick | `fly ssh console -a salon-poc -C "python -c \"from reminders import run_reminder_tick; print(run_reminder_tick())\""` |
| See what secrets are set (names only) | `fly secrets list -a salon-poc` |
| Scale up to a bigger VM | `fly scale vm shared-cpu-2x --memory 2048` |
| Restart the machine (no rebuild) | `fly apps restart salon-poc` |

### What lives where after deploy

- **Code**: baked into the container image at `/app`.
- **Salon data**: `/data/receptionist.db` (SQLite) on the `receptionist_data`
  volume. Persists across `fly deploy`s. Alembic migrations run on every
  boot. Edits made in the admin UI stick. The legacy workbook, if present,
  is one-shot imported and renamed to
  `/data/ReceptionistData.xlsx.imported`.
- **Piper voice**: `/app/voices/en_US-amy-medium.onnx` — pre-baked into
  the image at build time by the Dockerfile.
- **Logs**: `/data/logs/*.log` on the persistent volume (`LOG_DIR` in
  `fly.toml`). Rotated by size; retained ~14 days (`server.log`) /
  ~30 days (`transcripts.log`). Also streamed to Fly's log service
  (`fly logs`). Locally, defaults to `./logs`.

---

## The big-picture trade-offs we made

- **We use Deepgram for speech-to-text** so the app has no GPU requirement
  and can run on a $5/month Fly.io machine. Trade-off: ~$0.26/hour of call
  vs. free local Whisper — but no GPU, no Windows-only CUDA setup, and
  Deepgram is meaningfully more accurate on phone audio anyway.
- **We use Piper for text-to-speech** so there's no per-word TTS charge.
  Trade-off: less natural-sounding than ElevenLabs, but "free forever" on
  the CPU we already pay for.
- **We use Twilio as the phone-network bridge** instead of running our own
  SIP infrastructure. Trade-off: a few cents per minute, but no telecom
  expertise required.
- **The transport is swappable.** Browser, Twilio, and other phone providers
  all plug into the same pipeline. If we ever leave Twilio for a cheaper
  provider, it's a one-file change.
- **Data lives in SQLite on a persistent volume.** Trade-off: won't scale
  past one salon and one machine, but it's simple, zero-ops, and
  `export_xlsx` still gives you a spreadsheet backup whenever you want
  one. Migrating to Postgres is a connection-string change whenever we
  outgrow this.
