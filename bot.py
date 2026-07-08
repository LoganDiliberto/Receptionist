"""Funkle receptionist bot.

Builds and runs the voice pipeline for one call:
  transport.input -> Silero VAD -> faster-whisper STT -> OpenAI LLM -> Piper TTS -> transport.output

`run_bot(transport, sample_rate)` is transport-agnostic — `server.py` passes either
a SmallWebRTCTransport (browser test client) or a FastAPIWebsocketTransport (Twilio).
"""

from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

from loguru import logger


def _add_nvidia_dll_dirs() -> None:
    """Make CUDA DLLs from pip-installed nvidia-* wheels visible to ctranslate2.

    On Windows, Python 3.8+ ignores PATH for native DLL resolution and requires
    explicit `os.add_dll_directory()` calls. Without this, faster-whisper crashes
    with "Library cublas64_12.dll is not found or cannot be loaded".
    """
    if sys.platform != "win32":
        return
    site_packages = Path(__file__).parent / ".venv" / "Lib" / "site-packages" / "nvidia"
    import sysconfig
    extra_root = Path(sysconfig.get_paths()["purelib"]) / "nvidia"
    seen: set[str] = set()
    for root in (site_packages, extra_root):
        if not root.exists():
            continue
        for bin_dir in root.glob("*/bin"):
            p = str(bin_dir)
            if p in seen:
                continue
            seen.add(p)
            try:
                os.add_dll_directory(p)
            except (FileNotFoundError, OSError) as e:
                logger.warning(f"Could not add DLL dir {p}: {e}")
            # ctranslate2's .pyd uses standard Windows DLL resolution for
            # transitive deps (cublas -> cudart, cudnn -> cublas), which
            # honors PATH but ignores add_dll_directory. So set both.
            if p not in os.environ.get("PATH", "").split(os.pathsep):
                os.environ["PATH"] = p + os.pathsep + os.environ.get("PATH", "")


_add_nvidia_dll_dirs()


from datetime import date

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
from pipecat.services.openai.llm import OpenAILLMService
from pipecat.services.piper.tts import PiperTTSService
from pipecat.services.whisper.stt import WhisperSTTService
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


def _build_system_prompt() -> str:
    """Salon-aware system prompt. Built fresh each call so today's date is current."""
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


async def _handle_check_availability(params: FunctionCallParams) -> None:
    args = params.arguments
    result = await salon.check_availability(
        date_iso=args["date_iso"],
        stylist=args.get("stylist"),
        service=args.get("service"),
    )
    logger.info(f"check_availability({args}) -> {result}")
    await params.result_callback(result)


async def _handle_book_appointment(params: FunctionCallParams) -> None:
    args = params.arguments
    result = await salon.book_appointment(
        customer_name=args["customer_name"],
        customer_phone=args["customer_phone"],
        stylist=args["stylist"],
        service=args["service"],
        date_iso=args["date_iso"],
        time_str=args["time_str"],
    )
    logger.info(f"book_appointment({args}) -> {result}")
    await params.result_callback(result)


async def run_bot(transport: BaseTransport, *, sample_rate: int) -> None:
    """Build and run the voice pipeline against an already-connected transport.

    Args:
        transport: A constructed Pipecat transport (WebRTC, Twilio WS, etc).
        sample_rate: Wire sample rate (e.g. 16000 for WebRTC, 8000 for Twilio
            Media Streams). Services resample internally as needed.
    """
    session_id = uuid.uuid4().hex[:8]
    logger.info(f"Starting session {session_id} @ {sample_rate} Hz")

    # VAD tuning: defaults (stop_secs=0.2) end the turn after just 200ms of
    # silence, which clips natural mid-sentence pauses on a phone call. 0.8s
    # is forgiving enough for breath pauses while still feeling responsive.
    vad = VADProcessor(
        vad_analyzer=SileroVADAnalyzer(
            sample_rate=sample_rate,
            params=VADParams(confidence=0.7, start_secs=0.2, stop_secs=1.2, min_volume=0.5),
        )
    )

    # Whisper tuning:
    # - language=EN: pin to English so noisy phone audio can't drift detection.
    # - no_speech_prob=0.8: a segment is KEPT when segment.no_speech_prob is
    #   BELOW this threshold. Pipecat's default 0.4 is tuned for clean mic
    #   audio; phone audio (8 kHz μ-law) has higher baseline no_speech doubt,
    #   so we raise the bar to let real speech through.
    stt = WhisperSTTService(
        settings=WhisperSTTService.Settings(
            model=os.getenv("WHISPER_MODEL", "small.en"),
            language=Language.EN,
            no_speech_prob=0.8,
        ),
        device="cuda",
        compute_type="float16",
    )

    llm = OpenAILLMService(
        settings=OpenAILLMService.Settings(model=os.getenv("OPENAI_MODEL", "gpt-4o-mini")),
    )
    llm.register_function("check_availability", _handle_check_availability)
    llm.register_function("book_appointment", _handle_book_appointment)

    tts = PiperTTSService(
        settings=PiperTTSService.Settings(voice=os.getenv("PIPER_VOICE", "en_US-amy-medium")),
        download_dir=Path(__file__).parent / "voices",
        use_cuda=False,
    )

    context = LLMContext(
        messages=[{"role": "system", "content": _build_system_prompt()}],
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
