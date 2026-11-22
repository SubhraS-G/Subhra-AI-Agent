"""Voice layer for SURA: wake word, speaker verification, STT/TTS (Tier 3)."""

from system.voice.wake import (
    SpeakerVerifier,
    VoiceError,
    Voiceprint,
    cosine_similarity,
    is_wake,
    strip_wake,
    verifier_status,
)

__all__ = [
    "SpeakerVerifier", "VoiceError", "Voiceprint", "cosine_similarity",
    "is_wake", "strip_wake", "verifier_status",
]
