"""Wake word + speaker verification ("only my voice").

Wake behavior: SURA stays dormant until it hears its name (with fuzzy
matching for microphone noise), then activates for one command.

Voice ownership ("listen only to me"): SURA builds a voiceprint from a few
voice samples at enrollment (python -m cli enroll-voice). Every activation
is verified against that print before any command is processed; strangers
who say the wake word are politely ignored.

Design: the DSP math operates on raw float samples (list[float]) so the
whole module is unit-testable without any audio hardware. Microphone
capture, Vosk STT, and pyttsx3 TTS are imported lazily and are optional.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path

from system import paths
from system.logging_utils import log

SAMPLE_RATE = 16_000
ENERGY_FLOOR = 300.0          # below this RMS the mic is considered silent
DEFAULT_WAKE_PHRASES = ("sura", "hey sura", "hi sura", "yo sura")
# "SURA" is not an English word, so STT writes whatever sounds closest.
# These are distinctive enough to wake SURA on their own, anywhere in the text.
WAKE_TOKENS = {"sura", "soora", "sooora", "surah", "shura", "zura",
               "suda", "soral", "sorha", "suraa"}
GREETING_LEADS = ("hey", "hi", "yo", "ok", "okay", "hay", "oy", "a")
# Words that only count as the wake word directly after a greeting. Measured
# on this machine: TTS saying "hey sura what is the status" came back from
# whisper as "Hey, sir, what is the status?". So "hey sir" has to wake SURA,
# while a bare, mid-sentence "sir" (movies, other people talking) must not.
# Each of these was actually produced by whisper on this machine while the
# owner said "hey SURA" (base.en model): "hey sir", "hey so", "hey so rama",
# "hey soo", "hey surah", "surah", "sura". The greeting gate is what keeps
# them safe to accept.
GREETING_WAKES = {
    "sir", "sirs", "sara", "sarah", "sera", "sora", "zara", "suraj",
    "sirrah", "sirha", "surah", "soora", "shura", "zura", "suda", "sura",
    "so", "soo", "sor", "rama", "ram", "rahma", "suma", "sooraa",
}


class VoiceError(Exception):
    pass


# ---------------------------------------------------------------------------
# Wake matching

def extract_text(transcript: str) -> str:
    return "".join(ch for ch in transcript.lower() if ch.isalpha() or ch.isspace()).strip()


def is_wake(transcript: str, phrases: tuple[str, ...] = DEFAULT_WAKE_PHRASES) -> bool:
    """Fuzzy wake detection tolerant to STT misspellings of 'SURA'."""
    text = extract_text(transcript)
    if not text:
        return False
    words = text.split()
    if set(words) & WAKE_TOKENS:
        return True
    if len(words) >= 2 and words[0] in GREETING_LEADS and words[1] in GREETING_WAKES:
        return True
    for phrase in phrases:
        if phrase in text:
            return True
    return False


def strip_wake(transcript: str) -> str:
    """Remove the wake word and command prefixes, returning the bare command.

    Handles both halves of a misheard wake: the greeting ("hey sir …") and
    the token itself, whichever of the two STT dropped or mangled.
    """
    words = extract_text(transcript).split()
    # Loop: people repeat the wake phrase when SURA seems asleep
    # ("Hey, sir. Hey, sir."), and one pass left "hey sir" behind, which
    # then got sent to the brain as if it were the command.
    changed = True
    while changed and words:
        changed = False
        if len(words) >= 2 and words[0] in GREETING_LEADS and (
            words[1] in GREETING_WAKES or words[1] in WAKE_TOKENS
        ):
            words = words[1:]
            changed = True
        if words and (words[0] in WAKE_TOKENS or words[0] in GREETING_WAKES):
            words = words[1:]
            changed = True
    return " ".join(words).strip(" ,.!?")



# ---------------------------------------------------------------------------
# Voiceprint math (works on float samples in [-1, 1])

def _frames(samples: list[float], frame_size: int, hop: int) -> list[list[float]]:
    return [
        samples[i : i + frame_size]
        for i in range(0, len(samples) - frame_size + 1, hop)
    ]


_DFT_TABLES: dict = {}


def _dft_tables(frame_size: int, k_count: int) -> tuple[list, list]:
    """Cached cos/sin tables: tables[(frame_size, k)][k][i]."""
    key = (frame_size, k_count)
    if key not in _DFT_TABLES:
        cos_t, sin_t = [], []
        for k in range(1, k_count + 1):
            cos_t.append([math.cos(2 * math.pi * k * i / frame_size) for i in range(frame_size)])
            sin_t.append([math.sin(2 * math.pi * k * i / frame_size) for i in range(frame_size)])
        _DFT_TABLES[key] = (cos_t, sin_t)
    return _DFT_TABLES[key]


def _dft_magnitudes(frame: list[float], k_count: int) -> list[float]:
    """DFT magnitudes for k = 1..k_count with cached tables."""
    n = len(frame)
    cos_t, sin_t = _dft_tables(n, k_count)
    mags = []
    for k in range(k_count):
        cos_row, sin_row = cos_t[k], sin_t[k]
        real = sum(s * c for s, c in zip(frame, cos_row))
        imag = sum(s * s_ for s, s_ in zip(frame, sin_row))
        mags.append(math.sqrt(real * real + imag * imag) / n)
    return mags


def _mfcc_like(samples: list[float], n_filters: int = 16) -> list[float]:
    """Lightweight spectral voice signature.

    Per frame: band energy shares (sqrt-compressed) + spectral centroid.
    Shares (not raw logs) keep near-empty bands from dominating, and the
    centroid strongly separates speakers by pitch.
    """
    frame_size = 400          # 25 ms at 16 kHz (40 Hz per bin)
    hop = 320                 # 20 ms
    max_frames = 40           # analyze at most ~0.8s — speed
    frames = _frames(samples, frame_size, hop)[:max_frames]
    if not frames:
        return []
    k_count = 64             # up to 2560 Hz — covers voice fundamentals + formants
    acc = [0.0] * n_filters
    centroid_acc = 0.0
    for frame in frames:
        mags = _dft_magnitudes(frame, k_count)
        powers = [m * m for m in mags]
        total = sum(powers) + 1e-12
        band = max(1, len(powers) // n_filters)
        for b in range(n_filters):
            start, end = b * band, min((b + 1) * band, len(powers))
            if start >= end:
                continue
            acc[b] += math.sqrt(sum(powers[start:end]) / total)
        centroid_acc += sum((k + 1) * p for k, p in enumerate(powers)) / total
    n = len(frames)
    vector = [a / n for a in acc]
    vector.append(centroid_acc / n / k_count)   # normalized centroid
    return vector


def _rms(samples: list[float]) -> float:
    if not samples:
        return 0.0
    return math.sqrt(sum(s * s for s in samples) / len(samples))


def _normalize(samples: list[float]) -> list[float]:
    peak = max((abs(s) for s in samples), default=0.0)
    if peak < 1e-9:
        return samples
    return [s / peak for s in samples]


@dataclass
class Voiceprint:
    owner: str = "subhra"
    vector: list[float] = field(default_factory=list)
    sample_count: int = 0
    created: str = ""

    def to_json(self) -> str:
        return json.dumps({
            "owner": self.owner,
            "vector": self.vector,
            "sample_count": self.sample_count,
            "created": self.created,
        })

    @classmethod
    def from_json(cls, text: str) -> "Voiceprint":
        data = json.loads(text)
        return cls(**data)


def cosine_similarity(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def _configured_threshold() -> float:
    """Accept threshold from agent_config.yaml `voice_threshold` (default 0.90).

    A voiceprint enrolled under different conditions (distance, room, mic)
    from daily use yields low similarities even for the true owner, so the
    threshold is tunable from config instead of hard-coded.
    """
    from system.llm.client import CONFIG_FILE, _parse_simple_yaml

    try:
        raw = _parse_simple_yaml(
            CONFIG_FILE.read_text(encoding="utf-8")
        ).get("voice_threshold", "")
        value = float(raw)
    except (OSError, TypeError, ValueError):
        return 0.90
    return min(0.99, max(0.50, value))


class SpeakerVerifier:
    """Enrolls and verifies the owner's voiceprint (1 speaker by design)."""

    def __init__(self, path: Path | None = None, threshold: float | None = None) -> None:
        self.path = path or (paths.AGENT_ROOT / "voice" / "voiceprint.json")
        self.threshold = _configured_threshold() if threshold is None else threshold
        self.print_: Voiceprint | None = None
        if self.path.exists():
            try:
                self.print_ = Voiceprint.from_json(self.path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, TypeError):
                self.print_ = None

    @property
    def enrolled(self) -> bool:
        return self.print_ is not None and len(self.print_.vector) > 0

    def enroll(self, samples_list: list[list[float]], owner: str = "subhra") -> Voiceprint:
        """Build the print from several utterances of the owner's voice."""
        good = []
        for samples in samples_list:
            if len(samples) < SAMPLE_RATE // 2:      # ignore blips < 0.5 s
                continue
            if _rms(samples) < 0.005:                # ignore near-silence
                continue
            good.append(_mfcc_like(_normalize(samples)))
        if len(good) < 2:
            raise VoiceError(
                "Need at least 2 usable voice samples (>=0.5s, audible). "
                "Speak normally at a steady distance from the mic."
            )
        n = len(good)
        width = min(len(v) for v in good)
        vector = [sum(v[i] for v in good) / n for i in range(width)]
        import datetime as _dt

        self.print_ = Voiceprint(
            owner=owner,
            vector=vector,
            sample_count=n,
            created=_dt.date.today().isoformat(),
        )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(self.print_.to_json(), encoding="utf-8")
        log("voice_enrolled", owner=owner, samples=n)
        return self.print_

    def verify(self, samples: list[float]) -> tuple[bool, float]:
        """Return (is_owner, similarity). Unenrolled systems fail closed."""
        if not self.enrolled:
            raise VoiceError("No voiceprint enrolled. Run: python -m cli enroll-voice")
        probe = _mfcc_like(_normalize(samples))
        width = min(len(probe), len(self.print_.vector))
        if width == 0:
            return False, 0.0
        similarity = cosine_similarity(probe[:width], self.print_.vector[:width])
        return similarity >= self.threshold, round(similarity, 4)

    def score(self, samples: list[float]) -> float:
        """Similarity score without a threshold decision (for calibration)."""
        if not self.enrolled:
            raise VoiceError("No voiceprint enrolled. Run: python -m cli enroll-voice")
        probe = _mfcc_like(_normalize(samples))
        width = min(len(probe), len(self.print_.vector))
        return round(cosine_similarity(probe[:width], self.print_.vector[:width]), 4)


def verifier_status() -> dict:
    v = SpeakerVerifier()
    return {
        "enrolled": v.enrolled,
        "owner": v.print_.owner if v.enrolled else None,
        "samples": v.print_.sample_count if v.enrolled else 0,
        "created": v.print_.created if v.enrolled else None,
        "threshold": v.threshold,
        "path": str(v.path),
    }
