# Receptionist Project
An AI receptionist that answers a phone. Someone calls a real phone number, the
AI picks up, has a real conversation with them, takes a message, and hangs up.

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
5. The program runs each chunk through the pipeline above. When the caller
   pauses long enough, Whisper transcribes what they said and hands the text
   to the AI.
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
| `run.py` | Convenience launcher: spawns `server.py` and `ngrok http 7860` together and prints the public URL you paste into Twilio. Also `run.cmd` for a shorter double-click / one-word invocation on Windows. |
| `bot.py` | Defines the voice pipeline — the chain of components that turn caller audio into a reply. Transport-agnostic, so the same bot works for browser tests and real phone calls. |
| `salon.py` | The salon data layer. Reads and writes `ReceptionistData.xlsx` (hours, staff, services, schedules, appointments) and exposes the two async tools (`check_availability`, `book_appointment`) the LLM calls, plus CRUD helpers used by the admin API. |
| `calls.py` | Parses `logs/transcripts.log` into structured call records and links each call to the appointment it produced (via the `session_id` column). |
| `admin_api.py` | FastAPI router mounted at `/api` that exposes staff/services/hours/appointments/calls to the admin UI. |
| `admin-ui/` | Angular admin console (built with `npm run build`; served by FastAPI at `/admin`). |
| `static/index.html` | A small webpage that lets you "call" the bot from your browser for testing. |
| `voices/` | The Piper text-to-speech voice files. |
| `logs/server.log` | Everything the program logs, rotated. Useful when something breaks. |
| `logs/transcripts.log` | Just the conversation transcripts. One line per turn. Useful for tuning. |
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

### Real phone call

The easy way — one command starts both the server and ngrok, prints the
public URL you need for Twilio, and shuts everything down on Ctrl+C:

```
run
```

(or `.venv\Scripts\python.exe run.py` if you'd rather not use the `.cmd`
shortcut.) Copy the printed `Twilio webhook` URL into your Twilio number's
"A call comes in" webhook (POST) in the Twilio Console, then dial the
number. See `run.py` — it wraps `server.py` and `ngrok http 7860` so you
don't have to babysit two terminals.

If you'd rather run them yourself:

1. Start the server: `.venv\Scripts\python.exe server.py`
2. In a second terminal: `ngrok http 7860`
3. Copy the `https://*.ngrok-free.app` URL ngrok shows.
4. In the Twilio Console, open your number's settings. For "A call comes in",
   set the webhook to `https://<your-ngrok-url>/voice` (POST).
5. Dial your Twilio number from any phone.

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

### What the pages do

- **Dashboard** — quick counts of staff, services, and appointments plus the
  salon's location.
- **Staff** — add, edit, or remove staff members; pick which services they
  offer and set their weekly schedule.
- **Services** — add, edit, or remove services and their duration and price.
  Services live in a `Services` sheet created automatically on first boot.
- **Calendar** — a week-at-a-glance grid of upcoming appointments. Click any
  day to add a new booking, or click an existing appointment to edit or
  cancel it. Uses the same conflict-detection logic the voice bot does.
- **Calls** — an observability view of every call the bot has answered.
  Each row shows when the call happened, how long it lasted, how many turns
  it took, and whether it resulted in a booking. Click a row for the full
  transcript and a "view in calendar" link to any appointment the call
  produced. Data is derived from `logs/transcripts.log` plus the
  `session_id` column the bot now stamps on every appointment it books.

---

## How to tune it when it goes wrong

Watch `logs/transcripts.log` after a bad call. You'll see what the program
actually heard you say and what it actually replied. From there:

- **It heard the wrong words.** Try a phone-tuned Deepgram model —
  `DEEPGRAM_MODEL=nova-2-phonecall` is trained specifically on
  narrow-band phone audio. If a specific accent trips it up, boost
  key vocabulary via the `keywords` setting on `DeepgramSTTService`.
- **It heard you correctly but said something dumb.** Use a smarter language
  model (`OPENAI_MODEL=gpt-4o` in `.env`).
- **It cut you off mid-sentence.** Increase `stop_secs` in `bot.py` (the VAD
  setting). Higher means more pause tolerance, lower means snappier replies.
- **It didn't hear you at all.** Lower `confidence` (currently `0.7`) or
  `min_volume` (currently `0.5`) in the `VADParams` inside `bot.py` to let
  quieter or less-confident audio segments through.

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

Merges to `main` deploy automatically. This section is the one-time setup
you do before the first deploy works.

### 1. Install the Fly CLI

Once, on your dev machine:

```
# Windows (PowerShell)
iwr https://fly.io/install.ps1 -useb | iex

# macOS / Linux
curl -L https://fly.io/install.sh | sh
```

Then `fly auth login` and pick (or create) the org you want to bill.

### 2. Create the app and its persistent volume

From the project root, one time:

```
fly launch --no-deploy --copy-config --name funkle-receptionist
fly volumes create receptionist_data --region iad --size 1
```

`fly.toml` already has the volume mount, region, and machine size wired up
— `fly launch` just picks a name and creates the app record on Fly's side.
Change `funkle-receptionist` to whatever name is free; if you change it,
update the `app = "..."` line in `fly.toml` to match.

### 3. Set production secrets

Fly-managed secrets, injected as env vars at runtime — never committed:

```
fly secrets set \
  OPENAI_API_KEY=sk-... \
  DEEPGRAM_API_KEY=... \
  TWILIO_ACCOUNT_SID=AC... \
  TWILIO_AUTH_TOKEN=...
```

Any of the optional overrides (`OPENAI_MODEL`, `DEEPGRAM_MODEL`,
`PIPER_VOICE`, `LOG_LEVEL`) can be set the same way.

### 4. First manual deploy

Prove the container image builds and boots before hooking up CI:

```
fly deploy --remote-only
```

When it prints `Machine ... reached its target running state`, browse to
`https://funkle-receptionist.fly.dev/admin` and confirm the admin UI loads.

### 5. Point Twilio at the Fly URL

In the Twilio Console, edit your number's "A call comes in" webhook to
`https://<your-app>.fly.dev/voice` (POST). Dial it — you should hear the bot.
No more ngrok in production.

### 6. Turn on CI/CD

Deploy-on-merge lives at `.github/workflows/deploy.yml`. It runs
`flyctl deploy --remote-only` whenever `main` receives a push, using a
long-lived Fly API token stored as a GitHub Actions secret.

```
# Generate a deploy-only token bound to this app
fly tokens create deploy -a funkle-receptionist
```

Copy the token (starts with `FlyV1 fm2_...`). Then in GitHub:

1. Go to **Settings → Secrets and variables → Actions**.
2. Click **New repository secret**.
3. Name: `FLY_API_TOKEN`. Value: the token you just copied.

From now on: open a PR from any feature branch, get it reviewed, merge to
`main`, watch **Actions** show a green deploy, refresh the app URL. That's it.

### Day-to-day operations

| Task | Command |
|---|---|
| Tail production logs | `fly logs -a funkle-receptionist` |
| Open a shell in the running container | `fly ssh console -a funkle-receptionist` |
| Roll back to the previous release | `fly releases -a funkle-receptionist` then `fly deploy --image <previous-image-ref>` |
| Download the live salon workbook | `fly ssh sftp get /data/ReceptionistData.xlsx` |
| See what secrets are set (names only) | `fly secrets list -a funkle-receptionist` |
| Scale up to a bigger VM | `fly scale vm shared-cpu-2x --memory 2048` |

### What lives where after deploy

- **Code**: baked into the container image at `/app`.
- **Salon data**: `/data/ReceptionistData.xlsx` on the `receptionist_data`
  volume. Persists across `fly deploy`s. Edits made in the admin UI stick.
- **Logs**: `/app/logs/*.log` inside the container (rotated), plus everything
  streams to Fly's log service (queryable via `fly logs`). If you want the
  transcript log to survive redeploys, move `LOG_DIR` to `/data/logs/` in
  `server.py`.

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
- **Data lives in Excel on a persistent volume.** Trade-off: won't scale
  past one salon and one machine, but it *is* the file managers know how
  to open, edit, and back up. Migrating to Postgres is a straight
  `salon.py` refactor whenever we outgrow this.
