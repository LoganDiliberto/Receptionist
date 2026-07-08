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
import sys
from pathlib import Path

from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, Request, WebSocket
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from loguru import logger

from pipecat.serializers.twilio import TwilioFrameSerializer
from pipecat.transports.base_transport import BaseTransport, TransportParams
from pipecat.transports.smallwebrtc.request_handler import (
    SmallWebRTCConnection,
    SmallWebRTCPatchRequest,
    SmallWebRTCRequest,
    SmallWebRTCRequestHandler,
)
from pipecat.transports.smallwebrtc.transport import SmallWebRTCTransport
from pipecat.transports.websocket.fastapi import (
    FastAPIWebsocketParams,
    FastAPIWebsocketTransport,
)

from bot import run_bot

load_dotenv()

ROOT = Path(__file__).parent
STATIC_DIR = ROOT / "static"
LOG_DIR = ROOT / "logs"
LOG_DIR.mkdir(exist_ok=True)


def _is_transcript(record) -> bool:
    return record["extra"].get("transcript") is True


# Keep loguru's pre-configured stderr sink so existing console output is unchanged,
# but stop transcript-tagged messages from echoing twice. Then add two file sinks.
logger.remove()
logger.add(
    sys.stderr,
    filter=lambda r: not _is_transcript(r),
    level=os.getenv("LOG_LEVEL", "DEBUG"),
)
# Mirror transcript-tagged lines to stderr in a clean format too.
logger.add(
    sys.stderr,
    filter=_is_transcript,
    format="<green>{time:HH:mm:ss}</green> <cyan>[{extra[session]}]</cyan> "
           "<level>{extra[role]:>9}</level>: {message}",
    level="INFO",
)
# Full server log with rotation (10 MB, keep 5 files).
logger.add(
    LOG_DIR / "server.log",
    rotation="10 MB",
    retention=5,
    enqueue=True,  # async-safe: another thread does the write
    level="DEBUG",
)
# Dedicated conversation log — one line per turn, easy to grep.
logger.add(
    LOG_DIR / "transcripts.log",
    filter=_is_transcript,
    format="{time:YYYY-MM-DD HH:mm:ss} [{extra[session]}] {extra[role]:>9}: {message}",
    rotation="5 MB",
    retention=20,
    enqueue=True,
    level="INFO",
)
logger.info(f"Logging to {LOG_DIR}")

# WebRTC pipeline runs at 16 kHz (matches Whisper). Twilio Media Streams
# are 8 kHz μ-law; the serializer handles encoding and resampling.
WEBRTC_SAMPLE_RATE = 16000
TWILIO_SAMPLE_RATE = 8000

app = FastAPI(title="Funkle Receptionist")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

webrtc_handler = SmallWebRTCRequestHandler()


# ---------- Browser test client (WebRTC) ----------


@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


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
    to bidirectionally stream the call audio to our WebSocket endpoint."""

    # The wss URL needs to be the public-facing host (ngrok in dev). Trust
    # X-Forwarded-Host if present so ngrok works without configuration.
    host = request.headers.get("x-forwarded-host") or request.url.netloc
    ws_url = f"wss://{host}/twilio/ws"

    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Connect>
    <Stream url="{ws_url}" />
  </Connect>
</Response>"""
    logger.info(f"TwiML response — streaming to {ws_url}")
    return Response(content=twiml, media_type="application/xml")


@app.websocket("/twilio/ws")
async def twilio_media_stream(websocket: WebSocket) -> None:
    """One Twilio Media Stream per call. Twilio sends 'connected' then 'start'
    before any media — we parse the start frame for the stream/call SIDs, then
    hand the live socket to the bot pipeline."""

    await websocket.accept()
    logger.info("Twilio WS accepted")

    # Read until we get the 'start' event (skips the initial 'connected' event).
    stream_sid: str | None = None
    call_sid: str | None = None
    while stream_sid is None:
        raw = await websocket.receive_text()
        msg = json.loads(raw)
        if msg.get("event") == "start":
            stream_sid = msg["start"]["streamSid"]
            call_sid = msg["start"].get("callSid")
            logger.info(f"Twilio stream started: stream={stream_sid} call={call_sid}")

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

    await _safe_run_bot(transport, TWILIO_SAMPLE_RATE)


# ---------- Shared ----------


@app.on_event("shutdown")
async def _shutdown() -> None:
    await webrtc_handler.close()


async def _safe_run_bot(transport: BaseTransport, sample_rate: int) -> None:
    try:
        await run_bot(transport, sample_rate=sample_rate)
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
