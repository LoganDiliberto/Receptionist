"""Smoke test: make sure Whisper can actually transcribe on CUDA (not just load)."""
from bot import _add_nvidia_dll_dirs  # noqa: F401 — runs at import
import numpy as np
from faster_whisper import WhisperModel

print("Loading Whisper small.en on CUDA float16…")
m = WhisperModel("small.en", device="cuda", compute_type="float16")
print("Loaded. Running a fake transcription…")
# 1 second of silence at 16kHz — should produce empty/near-empty output but exercise CUDA kernels
audio = np.zeros(16000, dtype=np.float32)
segments, info = m.transcribe(audio, language="en")
segs = list(segments)
print(f"OK — got {len(segs)} segments. Detected language: {info.language}")
