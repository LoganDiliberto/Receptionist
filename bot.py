"""Funkle receptionist bot.

Builds and runs the voice pipeline for one call:
  transport.input -> Silero VAD -> Deepgram STT -> OpenAI LLM -> Piper TTS -> transport.output

`run_bot(transport, sample_rate)` is transport-agnostic — `server.py` passes either
a SmallWebRTCTransport (browser test client) or a FastAPIWebsocketTransport (Twilio).
"""

from __future__ import annotations

import os
import uuid
from datetime import date
from pathlib import Path

from loguru import logger

from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.adapters.schemas.tools_schema import ToolsSchema
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.frames.frames import LLMTextFrame, TranscriptionFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.transcriptions.language import Language
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import LLMContextAggregatorPair
from pipecat.processors.audio.vad_processor import VADProcessor
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.services.llm_service import FunctionCallParams
from pipecat.services.deepgram.stt import DeepgramSTTService
from pipecat.services.openai.llm import OpenAILLMService
from pipecat.services.piper.tts import PiperTTSService
from pipecat.transports.base_transport import BaseTransport

import salon

class TranscriptLogger(FrameProcessor):
    """Pass-through processor that logs what STT heard or what the LLM replied.

    Writes to a "transcript"-tagged loguru sink (configured in server.py) so
    the conversation log lands in logs/transcripts.log without the rest of the
    pipecat noise.
    """

    def __init__(self, role: str, frame_type: type, session_id: str) -> None:
        super().__init__()
        self._role = role  # "user" or "assistant"
        self._frame_type = frame_type
        self._session_id = session_id
        self._buffer = ""

    async def process_frame(self, frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if isinstance(frame, self._frame_type):
            text = getattr(frame, "text", "")
            if text:
                if self._frame_type is LLMTextFrame:
                    # LLM frames stream token-by-token; buffer until punctuation
                    # so the log shows complete sentences.
                    self._buffer += text
                    if text[-1] in ".?!\n":
                        self._emit(self._buffer.strip())
                        self._buffer = ""
                else:
                    self._emit(text)
        await self.push_frame(frame, direction)

    def _emit(self, text: str) -> None:
        logger.bind(transcript=True, session=self._session_id, role=self._role).info(text)


def _build_system_prompt(caller_phone: str | None) -> str:
    """Salon-aware system prompt. Built fresh each call so today's date is
    current and the caller-info block reflects who is on the line right now.
    """
    today = date.today()
    weekday = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"][
        today.weekday()
    ]
    return f"""You are Funkle, the phone receptionist for a hair salon.

TODAY'S DATE: {today.isoformat()} ({weekday})

Style rules — non-negotiable because your reply is spoken aloud over a phone:
- Keep replies short. One or two sentences. Never more than three.
- Plain spoken English. No markdown, no bullets, no emoji, no code.
- Say times naturally ("ten thirty" not "10:30"). Say dates naturally too
  ("Friday the twenty-sixth" not "2026-06-26"). But pass ISO format to tools.
- Confirm important details back to the caller before booking.

Behavior:
- Wait for the caller to speak first; do not introduce yourself unprompted.
- Help callers with three things: salon info, checking availability, and booking.
- If you don't know something, say so and offer to take a message.

Caller-specific behavior:
- If CALLER INFO below says the caller is on file, greet them by their
  first name once you know why they're calling, and DO NOT ask for their
  name or callback phone number — you already have both.
- DO NOT ask for a callback phone number for any caller. The number in
  CALLER INFO is the one to use — pass it as customer_phone when booking.
  The only exception is if the caller explicitly asks you to use a
  different number.
- If a returning caller says "my appointment" without naming which one
  and CALLER INFO shows exactly one upcoming appointment, assume that's
  the one they mean. If there are multiple, ask which.

Tool-use rules — these are MANDATORY:
1. NEVER claim a slot is taken, free, or that a stylist is "booked until X"
   from your own reasoning. You don't know — only the tool does. The instant
   the caller mentions ANY specific date or time, your next action must be
   to call check_availability. Do not narrate availability before the tool
   returns.
2. To book, you must collect: customer name, callback phone number, service,
   stylist, date, and time. Read those details back to the caller for
   confirmation, then call book_appointment. Trust its result — if it
   returns ok=true, the booking succeeded; if it returns an error, say so.
3. For static info (hours, services, which stylist offers what, weekly
   schedule) answer from the data block below — no tool call needed.

{salon.caller_context(caller_phone)}

{salon.system_prompt_context()}
"""


def _build_tools_schema() -> ToolsSchema:
    return ToolsSchema(standard_tools=[
        FunctionSchema(
            name="check_availability",
            description=(
                "Look up open appointment slots on a specific date. "
                "Use this BEFORE booking, and any time the caller asks 'when can I "
                "get in for X' or 'is Mandy free on Thursday'."
            ),
            properties={
                "date_iso": {
                    "type": "string",
                    "description": "Date in YYYY-MM-DD format. Resolve 'tomorrow', "
                                   "'Friday', etc. from TODAY'S DATE in the system prompt.",
                },
                "stylist": {
                    "type": "string",
                    "description": "Optional stylist name. Omit to check all stylists.",
                },
                "service": {
                    "type": "string",
                    "enum": ["cut", "color", "perm", "shave"],
                    "description": "Optional service filter. Affects slot length.",
                },
            },
            required=["date_iso"],
        ),
        FunctionSchema(
            name="book_appointment",
            description=(
                "Actually book an appointment. ONLY call this after confirming the "
                "slot with check_availability AND reading the details back to the "
                "caller for confirmation."
            ),
            properties={
                "customer_name": {"type": "string", "description": "Caller's full name."},
                "customer_phone": {
                    "type": "string",
                    "description": "Caller's callback phone number, digits only.",
                },
                "stylist": {"type": "string", "description": "Stylist name."},
                "service": {
                    "type": "string",
                    "enum": ["cut", "color", "perm", "shave"],
                },
                "date_iso": {"type": "string", "description": "Date in YYYY-MM-DD format."},
                "time_str": {
                    "type": "string",
                    "description": "Start time in 24-hour HH:MM format, e.g. '14:30'.",
                },
            },
            required=["customer_name", "customer_phone", "stylist", "service",
                      "date_iso", "time_str"],
        ),
    ])


def _make_tool_handlers(session_id: str, caller_phone: str | None):
    """Build tool handlers that close over the current session id and caller.

    Defining these inside ``run_bot`` (via this factory) gives us a clean
    way to attach the per-call session id + caller phone to every
    appointment the LLM books — without having to thread them through
    the LLM's own arguments (the LLM shouldn't have to think about
    ``client_id`` or session bookkeeping).
    """

    async def check_availability(params: FunctionCallParams) -> None:
        args = params.arguments
        result = await salon.check_availability(
            date_iso=args["date_iso"],
            stylist=args.get("stylist"),
            service=args.get("service"),
        )
        logger.bind(session=session_id).info(
            f"check_availability({args}) -> {result}"
        )
        await params.result_callback(result)

    async def book_appointment(params: FunctionCallParams) -> None:
        args = params.arguments
        result = await salon.book_appointment(
            customer_name=args["customer_name"],
            customer_phone=args["customer_phone"],
            stylist=args["stylist"],
            service=args["service"],
            date_iso=args["date_iso"],
            time_str=args["time_str"],
            session_id=session_id,
            caller_phone=caller_phone,
        )
        logger.bind(session=session_id).info(
            f"book_appointment({args}) -> {result}"
        )
        await params.result_callback(result)

    return check_availability, book_appointment


async def run_bot(
    transport: BaseTransport,
    *,
    sample_rate: int,
    caller_phone: str | None = None,
) -> None:
    """Build and run the voice pipeline against an already-connected transport.

    Args:
        transport: A constructed Pipecat transport (WebRTC, Twilio WS, etc).
        sample_rate: Wire sample rate (e.g. 16000 for WebRTC, 8000 for Twilio
            Media Streams). Services resample internally as needed.
        caller_phone: Caller's phone number (E.164 or any format), extracted
            from the Twilio ``From`` field on inbound calls. ``None`` for the
            browser test transport, where there is no phone number.
    """
    session_id = uuid.uuid4().hex[:8]
    normalized_caller = salon.normalize_phone(caller_phone) if caller_phone else None
    logger.info(
        f"Starting session {session_id} @ {sample_rate} Hz"
        + (f" (caller={salon.format_phone(normalized_caller)})" if normalized_caller else "")
    )

    # VAD tuning: defaults (stop_secs=0.2) end the turn after just 200ms of
    # silence, which clips natural mid-sentence pauses on a phone call. 0.8s
    # is forgiving enough for breath pauses while still feeling responsive.
    vad = VADProcessor(
        vad_analyzer=SileroVADAnalyzer(
            sample_rate=sample_rate,
            params=VADParams(confidence=0.7, start_secs=0.2, stop_secs=1.2, min_volume=0.5),
        )
    )

    # Deepgram tuning:
    # - model: nova-3-general handles both wideband (WebRTC 16 kHz) and phone
    #   (Twilio 8 kHz μ-law → linear16) audio well. Override with DEEPGRAM_MODEL
    #   to try nova-2-phonecall or another model if a specific accent trips it up.
    # - language=EN: pin to English so noisy phone audio can't drift detection.
    # - smart_format=True: converts spoken numbers/dates/times to readable text
    #   ("ten thirty" → "10:30") which the LLM parses more reliably.
    # - interim_results=False: we already do end-of-turn detection with Silero VAD
    #   below, so we only want committed transcripts flowing into the LLM.
    stt = DeepgramSTTService(
        api_key=os.environ["DEEPGRAM_API_KEY"],
        settings=DeepgramSTTService.Settings(
            model=os.getenv("DEEPGRAM_MODEL", "nova-3-general"),
            language=Language.EN,
            smart_format=True,
            interim_results=False,
            punctuate=True,
        ),
    )

    llm = OpenAILLMService(
        settings=OpenAILLMService.Settings(model=os.getenv("OPENAI_MODEL", "gpt-4o-mini")),
    )
    check_availability_tool, book_appointment_tool = _make_tool_handlers(
        session_id, caller_phone=normalized_caller,
    )
    llm.register_function("check_availability", check_availability_tool)
    llm.register_function("book_appointment", book_appointment_tool)

    tts = PiperTTSService(
        settings=PiperTTSService.Settings(voice=os.getenv("PIPER_VOICE", "en_US-amy-medium")),
        download_dir=Path(__file__).parent / "voices",
        use_cuda=False,
    )

    context = LLMContext(
        messages=[{"role": "system", "content": _build_system_prompt(normalized_caller)}],
        tools=_build_tools_schema(),
    )
    aggregators = LLMContextAggregatorPair(context)

    pipeline = Pipeline([
        transport.input(),
        vad,
        stt,
        TranscriptLogger("user", TranscriptionFrame, session_id),
        aggregators.user(),
        llm,
        TranscriptLogger("assistant", LLMTextFrame, session_id),
        tts,
        transport.output(),
        aggregators.assistant(),
    ])

    task = PipelineTask(
        pipeline,
        params=PipelineParams(
            audio_in_sample_rate=sample_rate,
            audio_out_sample_rate=sample_rate,
            enable_metrics=True,
        ),
    )

    @transport.event_handler("on_client_disconnected")
    async def _on_disconnected(_t, _client):
        logger.info("Client disconnected — cancelling pipeline")
        await task.cancel()

    runner = PipelineRunner(handle_sigint=False)
    await runner.run(task)
    logger.info(f"Session {session_id} ended")
