"""Speech IO: offline STT and TTS. Optional dependencies.

STT engine, in priority order:

1. **faster-whisper** (recommended — install with `pip install faster-whisper`):
   OpenAI Whisper running locally on CPU via CTranslate2. Vastly more accurate
   than Vosk, especially with Indian/non-US accents, and still fully offline.
   The `base.en` model (~145 MB, downloaded once on first use) is the default;
   set `SUBHRA_WHISPER_MODEL` (e.g. `small.en`) or `stt_model:` in
   `config/agent_config.yaml` to change it.
2. **Vosk** (fallback — the original ~50 MB model): noticeably weaker with
   accents; kept so voice mode still works where faster-whisper is absent.

The `Transcriber` class exposes the same streaming protocol for both engines:
`feed(pcm_bytes)` returns a final transcript when the utterance ends, and
`finish()` flushes the tail. Whisper has no native streaming endpoint, so the
implementation buffers PCM and re-transcribes a sliding window of recent
audio; anything the window re-decodes of already-emitted text is trimmed so
overlap duplicates never reach the command parser.

TTS: pyttsx3 (offline, SAPI5 on Windows). Everything here fails
loudly-but-gracefully when a dependency is missing so the rest of SURA keeps
working.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import zipfile
import urllib.request
from pathlib import Path

from system import paths
from system.logging_utils import log
from system.voice.wake import SAMPLE_RATE

MODEL_DIR = paths.AGENT_ROOT / "voice" / "models"
VOSK_MODEL_NAME = "vosk-model-small-en-us-0.15"
VOSK_MODEL_URL = f"https://alphacephei.com/vosk/models/{VOSK_MODEL_NAME}.zip"
WHISPER_MODEL_DIR = MODEL_DIR / "whisper"

# Whisper model selection ladder. tiny/base are fast but noticeably weaker
# with accents; small is the accuracy/speed sweet spot on CPU for a voice
# assistant. Env var (SUBHRA_WHISPER_MODEL) or config `stt_model:` wins.
WHISPER_SIZES = ("tiny.en", "base.en", "small.en", "medium.en")
DEFAULT_WHISPER_MODEL = "base.en"

# Streaming window tuning (seconds of audio).
WINDOW_MAX_S = 14.0        # force a decode when the window gets this long
SILENCE_SPLIT_S = 1.4      # internal pause long enough to close an utterance
ENERGY_SPEECH = 300.0      # RMS above this counts as sound (same as wake loop)


class SpeechError(Exception):
    pass


# ---------------------------------------------------------------------- STT

def model_path() -> Path:
    """Location of the legacy Vosk model (still used as the fallback engine)."""
    return MODEL_DIR / VOSK_MODEL_NAME


def model_installed() -> bool:
    return model_path().exists()


def download_model(progress=None) -> Path:
    """Download and extract the small Vosk model (~50 MB, one time).

    Kept for the fallback engine. faster-whisper models download themselves
    on first transcription (with their own progress display).
    """
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    target = model_path()
    if target.exists():
        return target
    archive = MODEL_DIR / f"{VOSK_MODEL_NAME}.zip"
    log("stt_model_download", url=VOSK_MODEL_URL)

    def hook(blocks: int, block_size: int, total: int):
        if progress and total:
            progress(min(100, blocks * block_size * 100 // total))

    urllib.request.urlretrieve(VOSK_MODEL_URL, archive, reporthook=hook)
    with zipfile.ZipFile(archive) as zf:
        zf.extractall(MODEL_DIR)
    archive.unlink()
    if not target.exists():
        raise SpeechError(f"model archive did not contain {VOSK_MODEL_NAME}")
    return target


def stt_engine_name() -> str:
    """Which STT engine will actually be used ('whisper' or 'vosk')."""
    try:
        import faster_whisper  # noqa: F401

        return "whisper"
    except ImportError:
        return "vosk"


def _pcm_to_wav_bytes(pcm: bytes, samplerate: int = SAMPLE_RATE) -> bytes:
    """Wrap raw int16 mono PCM in a WAV container (stdlib `wave`)."""
    import io
    import wave

    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(samplerate)
        wav.writeframes(pcm)
    return buf.getvalue()


def _default_whisper_model() -> str:
    """Resolve the Whisper model: config file, then env var, then default."""
    chosen = ""
    config = paths.AGENT_ROOT / "config" / "agent_config.yaml"
    try:
        if config.exists():
            for line in config.read_text(encoding="utf-8").splitlines():
                if line.strip().startswith("stt_model:"):
                    chosen = line.split(":", 1)[1].strip().strip('"').strip("'")
                    break
    except OSError:
        pass
    if not chosen:
        import os

        chosen = os.environ.get("SUBHRA_WHISPER_MODEL", "")
    return chosen.strip() or DEFAULT_WHISPER_MODEL


class Transcriber:
    """Streaming transcriber over faster-whisper, with Vosk fallback.

    Accepts int16 PCM byte chunks (16 kHz mono), exactly like the old Vosk
    implementation, so the voice loop and all tests keep working unchanged.
    """

    _floor: float = 400.0    # class default; instance value set in __init__

    def __init__(self) -> None:
        self._vosk = None        # KaldiRecognizer when running the fallback
        self._model = None       # WhisperModel otherwise
        try:
            self._model = self._make_whisper()
        except SpeechError:
            if model_installed():
                self._vosk = self._make_vosk()
            else:
                raise
        self._prefix = ""        # transcript already handed out as final
        self._buffer = b""       # PCM since the last final
        self._last_sound = 0.0   # wall clock of last non-silent chunk
        self._speech_seen = False
        self._started = False
        self._floor = 400.0      # adaptive noise floor (RMS), pulled down by quiet chunks
        self._engine = "vosk" if self._vosk is not None else "whisper"
        self._last_failure_log = 0.0

    # ---------------------------------------------------------- engine setup

    @staticmethod
    def _make_whisper():
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:
            raise SpeechError(
                "faster-whisper is not installed. Run: pip install faster-whisper"
            ) from exc
        name = _default_whisper_model()
        log("stt_engine_init", engine="whisper", model=name)
        # int8 on CPU: several times faster than float32 with no audible
        # accuracy loss for short commands.
        return WhisperModel(name, device="cpu", compute_type="int8",
                            download_root=str(WHISPER_MODEL_DIR))

    @staticmethod
    def _make_vosk():
        try:
            from vosk import KaldiRecognizer, Model
        except ImportError as exc:
            raise SpeechError(
                "No STT available: install faster-whisper (recommended, "
                "pip install faster-whisper) or vosk + the model "
                "(python -m cli listen --download-model)."
            ) from exc
        log("stt_engine_init", engine="vosk", model=VOSK_MODEL_NAME)
        model = Model(str(model_path()))
        recognizer = KaldiRecognizer(model, SAMPLE_RATE)
        recognizer.SetWords(False)
        return recognizer

    # ------------------------------------------------------------ streaming

    def feed(self, chunk: bytes) -> str | None:
        """Feed one PCM chunk; returns a final transcript when the utterance ends.

        Whisper protocol: buffer audio; when speech is followed by ~1.4 s of
        silence (or the window grows very long), decode the window and emit
        it as final. This mirrors what Vosk's endpointer did.
        """
        if self._vosk is not None:
            if self._vosk.AcceptWaveform(chunk):
                return self._text(self._vosk.Result())
            return None

        self._buffer += chunk
        now = time.monotonic()
        if not self._started:
            self._started = True
            self._last_sound = now
        rms = rms_of_pcm(chunk)
        # Adaptive gate: quiet mics (laptop arrays often peak under 150 RMS)
        # must still count as speech. Quiet chunks pull the floor down
        # (fast enough to adapt within ~1 s of dormancy); speech is anything
        # clearly above the floor, capped at the fixed ceiling so a slow
        # start never deafens SURA on a loud mic.
        if rms < self._floor:
            self._floor = max(50.0, self._floor * 0.94 + rms * 0.06)
        threshold = max(110.0, min(ENERGY_SPEECH, self._floor * 2.5))
        if rms > threshold:
            self._last_sound = now
            self._speech_seen = True
        if not self._speech_seen:
            # Trim pure leading silence so the window never grows unbounded
            # while SURA sits dormant.
            cap = SAMPLE_RATE * 2 * 30                    # 30 s cap
            if len(self._buffer) > cap:
                self._buffer = self._buffer[-SAMPLE_RATE * 2 * 5:]
            return None

        seconds = len(self._buffer) / (2 * SAMPLE_RATE)
        silent_for = now - self._last_sound
        if silent_for < SILENCE_SPLIT_S and seconds < WINDOW_MAX_S:
            return None

        try:
            text = self._decode_window()
        except Exception as exc:  # noqa: BLE001 — a broken decoder must not end the loop
            self._handle_decode_failure(exc)
            return None
        if not text:
            # Nothing new beyond what was already emitted — stop decoding
            # this window and wait for the next sound instead of burning
            # CPU re-transcribing the same audio every chunk.
            self._speech_seen = False
            return None
        return self._emit(text)

    def finish(self) -> str:
        """Flush whatever is still buffered as a final transcript."""
        if self._vosk is not None:
            return self._text(self._vosk.FinalResult())
        if not self._speech_seen or not self._buffer:
            return ""
        try:
            text = self._decode_window()
        except Exception as exc:  # noqa: BLE001 — same guarantee as feed()
            self._handle_decode_failure(exc)
            return ""
        self._buffer = b""
        return self._emit(text) if text else ""

    # ----------------------------------------------------------- internals

    def _decode_window(self) -> str:
        """Transcribe the buffered audio, keep only the fresh tail."""
        text = self._decode_pcm(self._buffer)
        if not text:
            return ""
        text = text.strip()
        prefix = self._prefix.strip().lower()
        if prefix and text.lower().startswith(prefix):
            text = text[len(self._prefix.strip()):]
        return text.strip(" ,.!").strip()

    def _emit(self, text: str) -> str:
        """Accept a window decode as final; remember it as the emitted prefix."""
        self._prefix = (self._prefix + " " + text).strip()
        self._buffer = b""
        self._speech_seen = False
        return text

    def _decode_pcm(self, pcm: bytes) -> str:
        """Transcribe raw 16 kHz mono PCM.

        Audio goes in as a float32 array on purpose. faster-whisper only uses
        its PyAV decoder for file/byte input, and that decoder is fragile
        across PyAV builds — an installed `av` without the `metadata_errors`
        argument made every decode raise `TypeError: open() got an unexpected
        keyword argument 'metadata_errors'`, which killed the voice loop
        before it transcribed a single word. An array skips that code path
        and saves a decode step.
        """
        try:
            import numpy as np

            audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
        except ImportError:      # numpy missing: the byte path is the only option
            audio = _pcm_to_wav_bytes(pcm)
        segments, _info = self._model.transcribe(
            audio,
            language="en",
            beam_size=1,                      # latency over a hair of accuracy
            vad_filter=True,                  # skip internal silence
            condition_on_previous_text=False, # no repetition loops
        )
        return " ".join(seg.text for seg in segments).strip()

    def _handle_decode_failure(self, exc: Exception) -> None:
        """Keep SURA hearing commands even when the whisper decoder breaks.

        Logs the failure (rate-limited) and, when the Vosk model is on disk,
        switches engines on the spot: degraded accuracy is a far better
        outcome than an assistant that has gone silently deaf.
        """
        now = time.monotonic()
        if now - self._last_failure_log > 60:
            self._last_failure_log = now
            log("stt_decode_error", engine=self._engine,
                error=f"{type(exc).__name__}: {exc}")
        if self._engine == "whisper" and model_installed():
            self._model = None
            self._vosk = self._make_vosk()
            self._engine = "vosk"
            self._buffer = b""
            self._speech_seen = False
            log("stt_engine_switch", to="vosk", reason=type(exc).__name__)

    @staticmethod
    def _text(raw: str) -> str:
        try:
            return json.loads(raw).get("text", "")
        except json.JSONDecodeError:
            return ""


# ---------------------------------------------------------------------- TTS

INLINE_ENV = "SUBHRA_TTS_INLINE"     # set for the helper process (stops spawning)


class Speaker:
    """Offline text-to-speech via pyttsx3 (SAPI5 on Windows).

    Every utterance is spoken by a fresh helper process, because speech only
    works once per process on this platform. Measured: the same line took
    4.85s (real speech), then 0.16s, then 0.12s — and a brand-new engine
    object in the same process did not help either. Result: SURA answered
    once and then silently never spoke again, which is exactly how the "it
    replied, then went quiet" bug looked from the outside.

    A process spawn costs ~0.4s and is dwarfed by the speaking time.
    """

    def __init__(self, rate: int = 178, volume: float = 1.0) -> None:
        try:
            import pyttsx3
        except ImportError as exc:
            raise SpeechError(
                "pyttsx3 is not installed. Run: pip install pyttsx3"
            ) from exc
        self._pyttsx3 = pyttsx3
        self.rate = rate
        self.volume = volume
        self._engine = None

    def say(self, text: str) -> None:
        if not text.strip():
            return
        log("speak", chars=len(text))
        if not os.environ.get(INLINE_ENV) and self._spawn(text):
            return
        self.speak_inline(text)

    def _spawn(self, text: str) -> bool:
        """Speak through `cli say` in a fresh process; True when it worked."""
        try:
            creation_no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            result = subprocess.run(
                [sys.executable, "-m", "cli", "say", text],
                cwd=str(paths.AGENT_ROOT),
                creationflags=creation_no_window,
                env={**os.environ, INLINE_ENV: "1", "PYTHONIOENCODING": "utf-8"},
                check=False,
            )
        except Exception as exc:  # noqa: BLE001 — fall back to in-process speech
            log("tts_spawn_failed", error=f"{type(exc).__name__}: {exc}")
            return False
        if result.returncode != 0:
            log("tts_spawn_failed", code=result.returncode)
            return False
        return True

    def speak_inline(self, text: str) -> None:
        """The in-process path — used inside the helper process itself."""
        if self._engine is None:
            engine = self._pyttsx3.init()
            engine.setProperty("rate", self.rate)
            engine.setProperty("volume", self.volume)
            self._engine = engine
        self._engine.say(text)
        self._engine.runAndWait()

    def stop(self) -> None:
        if self._engine is None:
            return
        try:
            self._engine.stop()
        except Exception:  # noqa: BLE001
            pass


# ------------------------------------------------------------------- mic

class MicrophoneStream:
    """Raw int16 PCM from the default mic via sounddevice."""

    def __init__(self, samplerate: int = SAMPLE_RATE, blocksize: int = 800) -> None:
        try:
            import sounddevice
        except ImportError as exc:
            raise SpeechError(
                "sounddevice is not installed. Run: pip install sounddevice numpy"
            ) from exc
        self._sd = sounddevice
        self.samplerate = samplerate
        self.blocksize = blocksize
        self._stream = None

    def __enter__(self) -> "MicrophoneStream":
        self._stream = self._sd.RawInputStream(
            samplerate=self.samplerate,
            blocksize=self.blocksize,
            dtype="int16",
            channels=1,
        )
        self._stream.start()
        return self

    def __exit__(self, *exc) -> None:
        if self._stream:
            self._stream.stop()
            self._stream.close()

    def read(self) -> bytes:
        data, _overflowed = self._stream.read(self.blocksize)
        return bytes(data)


def rms_of_pcm(pcm: bytes) -> float:
    """RMS energy of int16 PCM bytes (stdlib struct; audioop was removed in 3.14)."""
    import struct as _struct

    count = len(pcm) // 2
    if count == 0:
        return 0.0
    samples = _struct.unpack(f"<{count}h", pcm[: count * 2])
    return (sum(s * s for s in samples) / count) ** 0.5


def pcm_to_float(pcm: bytes) -> list[float]:
    count = len(pcm) // 2
    if count == 0:
        return []
    import struct as _struct

    return [s / 32768.0 for s in _struct.unpack(f"<{count}h", pcm[: count * 2])]


def float_to_pcm(samples: list[float]) -> bytes:
    import struct as _struct

    clamped = [max(-1.0, min(1.0, s)) for s in samples]
    return _struct.pack(f"<{len(clamped)}h", *(int(s * 32767) for s in clamped))
