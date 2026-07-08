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
   │  2. Speech-to-Text  (faster-whisper, local GPU) │
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
| `bot.py` | Defines the voice pipeline — the chain of components that turn caller audio into a reply. Transport-agnostic, so the same bot works for browser tests and real phone calls. |
| `salon.py` | The salon data layer. Reads and writes `ReceptionistData.xlsx` (hours, staff, services, schedules, appointments) and exposes the two async tools (`check_availability`, `book_appointment`) the LLM calls, plus CRUD helpers used by the admin API. |
| `admin_api.py` | FastAPI router mounted at `/api` that exposes staff/services/hours/appointments CRUD to the admin UI. |
| `admin-ui/` | Angular admin console (built with `npm run build`; served by FastAPI at `/admin`). |
| `static/index.html` | A small webpage that lets you "call" the bot from your browser for testing. |
| `voices/` | The Piper text-to-speech voice files. |
| `logs/server.log` | Everything the program logs, rotated. Useful when something breaks. |
| `logs/transcripts.log` | Just the conversation transcripts. One line per turn. Useful for tuning. |
| `.env` | Secrets and settings: your OpenAI API key, Twilio credentials, model choices. Not committed to git. |
| `.env.example` | A template showing what `.env` should contain. |

---

## What you need to run it

- A Windows PC with an NVIDIA GPU (3060-class or better).
- Python 3.11.
- An OpenAI API key.
- A Twilio account with a phone number (for real calls — browser testing
  doesn't need this).
- `ngrok` (so Twilio can reach your laptop over the public internet during
  development).

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

1. Start the server (same command as above).
2. In a second terminal, start ngrok pointing at port 7860:
   ```
   ngrok http 7860
   ```
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

---

## How to tune it when it goes wrong

Watch `logs/transcripts.log` after a bad call. You'll see what the program
actually heard you say and what it actually replied. From there:

- **It heard the wrong words.** Speech recognition needs help. Try a bigger
  Whisper model (`WHISPER_MODEL=medium.en` or `large-v3-turbo` in `.env`),
  or switch to a paid speech-to-text service like Deepgram which is built
  for phone audio.
- **It heard you correctly but said something dumb.** Use a smarter language
  model (`OPENAI_MODEL=gpt-4o` in `.env`).
- **It cut you off mid-sentence.** Increase `stop_secs` in `bot.py` (the VAD
  setting). Higher means more pause tolerance, lower means snappier replies.
- **It didn't hear you at all.** Lower `no_speech_prob` is *more* strict;
  raise it (closer to 1.0) to let quieter speech through. The current
  value is `0.8`.

---

## How much it costs to run

- **OpenAI** (gpt-4o-mini): about a tenth of a cent per minute of conversation.
- **Twilio** (US local number, inbound): $1.15 per month plus $0.0085 per minute.
- **Speech-to-text** (Whisper, local): free. Uses your GPU.
- **Text-to-speech** (Piper, local): free. Uses your CPU.
- **ngrok** (development): free tier is fine. Stable URLs cost $8/month.

A 5-minute call costs you roughly five cents.

---

## The big-picture trade-offs we made

- **We run our own speech-to-text and text-to-speech** so the bot has no
  per-call cost beyond the AI itself. Trade-off: Whisper is OK but not
  great on phone audio; a paid service like Deepgram is meaningfully
  more accurate.
- **We use Twilio as the phone-network bridge** instead of running our own
  SIP infrastructure. Trade-off: a few cents per minute, but no telecom
  expertise required.
- **The transport is swappable.** Browser, Twilio, and other phone providers
  all plug into the same pipeline. If we ever leave Twilio for a cheaper
  provider, it's a one-file change.
