"""FastAPI signaling server for the Funkle receptionist.

Routes:
  GET  /              -> serves static/index.html (browser test client)
  POST /offer         -> WebRTC SDP offer; spawns a bot for the new browser call
  PATCH /offer        -> trickle ICE candidates from the browser
  POST /voice         -> Twilio voice webhook; returns TwiML to start a Media Stream
  WS   /twilio/ws     -> Twilio Media Stream; runs a bot for the live phone call

Run:
  python server.py
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

from dotenv import load_dotenv

# Load env, then arm loguru *before* importing salon/db/bot so their
# import-time messages land in server.log (and on the /data volume in prod).
load_dotenv()

from log_config import configure_logging  # noqa: E402

configure_logging()

from fastapi import BackgroundTasks, FastAPI, Request, WebSocket  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
from fastapi.responses import FileResponse, Response  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from loguru import logger  # noqa: E402

from admin_api import router as admin_router  # noqa: E402

from pipecat.serializers.twilio import TwilioFrameSerializer  # noqa: E402
from pipecat.transports.base_transport import BaseTransport, TransportParams  # noqa: E402
from pipecat.transports.smallwebrtc.request_handler import (  # noqa: E402
    SmallWebRTCConnection,
    SmallWebRTCPatchRequest,
    SmallWebRTCRequest,
    SmallWebRTCRequestHandler,
)
from pipecat.transports.smallwebrtc.transport import SmallWebRTCTransport  # noqa: E402
from pipecat.transports.websocket.fastapi import (  # noqa: E402
    FastAPIWebsocketParams,
    FastAPIWebsocketTransport,
)

from auth import AdminBasicAuthMiddleware, log_auth_status  # noqa: E402
from bot import run_bot  # noqa: E402

log_auth_status()

ROOT = Path(__file__).parent
STATIC_DIR = ROOT / "static"
# Built Angular admin app. Empty until you run `npm run build` in admin-ui/.
ADMIN_DIST = ROOT / "admin-ui" / "dist" / "admin-ui" / "browser"

# WebRTC pipeline runs at 16 kHz (matches Whisper). Twilio Media Streams
# are 8 kHz μ-law; the serializer handles encoding and resampling.
WEBRTC_SAMPLE_RATE = 16000
TWILIO_SAMPLE_RATE = 8000

app = FastAPI(title="Funkle Receptionist")

# Admin Basic Auth. Added BEFORE CORS so a 401 is returned without CORS
# complication for unauthenticated probes. Protects /admin and /api only;
# /voice, /twilio/ws, /, /offer stay public.
app.add_middleware(AdminBasicAuthMiddleware)

# The Angular dev server runs on 4200; allow it (and any localhost origin
# during development) to talk to the API. Same-origin requests from the
# built /admin app aren't affected by this middleware.
# Expose Authorization so the browser can send Basic credentials from ng serve
# if a dev interceptor attaches them.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:4200",
        "http://127.0.0.1:4200",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*", "Authorization"],
)

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
app.include_router(admin_router)


# ---------- Admin UI (built Angular app) ----------


if ADMIN_DIST.exists():
    @app.get("/admin", include_in_schema=False)
    @app.get("/admin/{path:path}", include_in_schema=False)
    async def _admin_spa(path: str = "") -> Response:
        # Serve a real file when it exists (JS bundle, CSS, favicon, etc).
        # Otherwise fall through to index.html so Angular's client-side router
        # can handle deep links like /admin/staff on a hard refresh.
        if path:
            candidate = (ADMIN_DIST / path).resolve()
            if candidate.is_file() and ADMIN_DIST.resolve() in candidate.parents:
                return FileResponse(candidate)
        return FileResponse(ADMIN_DIST / "index.html")
else:
    @app.get("/admin", include_in_schema=False)
    @app.get("/admin/{_path:path}", include_in_schema=False)
    async def _admin_not_built(_path: str = "") -> Response:
        return Response(
            content=(
                "Admin UI is not built yet.\n"
                "Run:\n"
                "  cd admin-ui && npm install && npm run build\n"
            ),
            media_type="text/plain",
            status_code=503,
        )


webrtc_handler = SmallWebRTCRequestHandler()


# ---------- Browser test client (WebRTC) ----------


@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/favicon.ico", include_in_schema=False)
async def favicon() -> FileResponse:
    # Browsers request /favicon.ico by default even when the page doesn't
    # reference it. Serve the JPG favicon from the admin-ui source (which is
    # the single source of truth) with the right media type.
    return FileResponse(
        ROOT / "admin-ui" / "src" / "favicon.jpg",
        media_type="image/jpeg",
    )


@app.post("/offer")
async def offer(request: SmallWebRTCRequest, background: BackgroundTasks) -> dict:
    async def on_connection(conn: SmallWebRTCConnection) -> None:
        transport = SmallWebRTCTransport(
            webrtc_connection=conn,
            params=TransportParams(
                audio_in_enabled=True,
                audio_in_sample_rate=WEBRTC_SAMPLE_RATE,
                audio_out_enabled=True,
                audio_out_sample_rate=WEBRTC_SAMPLE_RATE,
            ),
        )
        background.add_task(_safe_run_bot, transport, WEBRTC_SAMPLE_RATE)

    return await webrtc_handler.handle_web_request(
        request=request,
        webrtc_connection_callback=on_connection,
    )


@app.patch("/offer")
async def offer_patch(request: SmallWebRTCPatchRequest) -> dict:
    await webrtc_handler.handle_patch_request(request)
    return {"status": "ok"}


# ---------- Real phone calls (Twilio Media Streams) ----------


@app.post("/voice")
async def twilio_voice_webhook(request: Request) -> Response:
    """Twilio hits this when a call comes in. We return TwiML telling Twilio
    to bidirectionally stream the call audio to our WebSocket endpoint.

    Twilio's webhook body includes ``From=+1XXXXXXXXXX`` — the caller's
    phone number. We forward it into the Media Stream via a
    ``<Parameter>`` tag so the bot can look up the caller in the
    Clients table and personalize its greeting.
    """

    # The wss URL needs to be the public-facing host (ngrok in dev). Trust
    # X-Forwarded-Host if present so ngrok works without configuration.
    host = request.headers.get("x-forwarded-host") or request.url.netloc
    ws_url = f"wss://{host}/twilio/ws"

    # Twilio posts as application/x-www-form-urlencoded. Fall back to
    # empty string so a bogus request without a From field doesn't blow
    # up the whole webhook.
    form = await request.form()
    caller_from = str(form.get("From") or "").strip()

    # Only emit the <Parameter> tag when we actually have a value —
    # missing tags are cleaner than empty ones and safer if Twilio ever
    # tightens its TwiML validation. XML-escape the value just in case.
    param_line = ""
    if caller_from:
        escaped = (
            caller_from.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;")
        )
        param_line = f'\n      <Parameter name="from" value="{escaped}" />'

    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Connect>
    <Stream url="{ws_url}">{param_line}
    </Stream>
  </Connect>
</Response>"""
    logger.info(
        f"TwiML response — streaming to {ws_url}"
        + (f" (From={caller_from})" if caller_from else " (no From field)")
    )
    return Response(content=twiml, media_type="application/xml")


@app.websocket("/twilio/ws")
async def twilio_media_stream(websocket: WebSocket) -> None:
    """One Twilio Media Stream per call. Twilio sends 'connected' then 'start'
    before any media — we parse the start frame for the stream/call SIDs and
    the caller's phone (forwarded via <Parameter name="from"> in the TwiML),
    then hand the live socket to the bot pipeline."""

    await websocket.accept()
    logger.info("Twilio WS accepted")

    # Read until we get the 'start' event (skips the initial 'connected' event).
    stream_sid: str | None = None
    call_sid: str | None = None
    caller_phone: str | None = None
    while stream_sid is None:
        raw = await websocket.receive_text()
        msg = json.loads(raw)
        if msg.get("event") == "start":
            start = msg["start"]
            stream_sid = start["streamSid"]
            call_sid = start.get("callSid")
            # customParameters carries whatever we put in <Parameter> tags
            # inside <Stream>. See /voice above. Twilio flattens them into
            # a dict keyed by the tag's name attribute.
            custom = start.get("customParameters") or {}
            caller_phone = (custom.get("from") or "").strip() or None
            logger.info(
                f"Twilio stream started: stream={stream_sid} call={call_sid}"
                + (f" from={caller_phone}" if caller_phone else "")
            )

    serializer = TwilioFrameSerializer(
        stream_sid=stream_sid,
        call_sid=call_sid,
        account_sid=os.getenv("TWILIO_ACCOUNT_SID"),
        auth_token=os.getenv("TWILIO_AUTH_TOKEN"),
    )

    transport = FastAPIWebsocketTransport(
        websocket=websocket,
        params=FastAPIWebsocketParams(
            audio_in_enabled=True,
            audio_in_sample_rate=TWILIO_SAMPLE_RATE,
            audio_out_enabled=True,
            audio_out_sample_rate=TWILIO_SAMPLE_RATE,
            add_wav_header=False,
            serializer=serializer,
        ),
    )

    await _safe_run_bot(
        transport,
        TWILIO_SAMPLE_RATE,
        caller_phone=caller_phone,
        call_sid=call_sid,
    )


# ---------- Shared ----------


_reminder_task: asyncio.Task | None = None


async def _reminder_loop() -> None:
    """Background poller: every REMINDER_POLL_SECONDS, try to send due SMS reminders.

    Never raises out of the loop — a bad Twilio tick must not take down the
    voice server. See reminders.py for the selection + send logic.
    """
    poll_seconds = int(os.getenv("REMINDER_POLL_SECONDS", "300"))
    # Small delay so the first tick doesn't race alembic / import on boot.
    await asyncio.sleep(15)
    logger.info(f"Reminder poller started (every {poll_seconds}s)")
    while True:
        try:
            summary = await asyncio.to_thread(_run_reminder_tick)
            if summary.get("due"):
                logger.info(f"Reminder tick result: {summary}")
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Reminder tick crashed; will retry next cycle")
        try:
            await asyncio.sleep(poll_seconds)
        except asyncio.CancelledError:
            raise


def _run_reminder_tick() -> dict:
    # Local import keeps server.py importable even if reminders deps shift.
    from reminders import run_reminder_tick
    return run_reminder_tick()


@app.on_event("startup")
async def _startup() -> None:
    global _reminder_task
    _reminder_task = asyncio.create_task(_reminder_loop(), name="reminder-poller")


@app.on_event("shutdown")
async def _shutdown() -> None:
    global _reminder_task
    if _reminder_task is not None:
        _reminder_task.cancel()
        try:
            await _reminder_task
        except asyncio.CancelledError:
            pass
        _reminder_task = None
    await webrtc_handler.close()


async def _safe_run_bot(
    transport: BaseTransport,
    sample_rate: int,
    *,
    caller_phone: str | None = None,
    call_sid: str | None = None,
) -> None:
    try:
        await run_bot(
            transport,
            sample_rate=sample_rate,
            caller_phone=caller_phone,
            call_sid=call_sid,
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("Bot crashed")


if __name__ == "__main__":
    import uvicorn

    host = os.getenv("HOST", "127.0.0.1")
    port = int(os.getenv("PORT", "7860"))
    logger.info(f"Funkle receptionist listening on http://{host}:{port}")
    uvicorn.run("server:app", host=host, port=port, reload=False, log_level="info")
