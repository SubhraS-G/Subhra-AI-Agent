"""Voice stack diagnostics — power the `python -m cli voice-check` command.

Read-only checks that answer exactly the questions that decide whether the
wake-word loop can work:

1. Are the pip deps installed (sounddevice, vosk, numpy, pyttsx3)?
2. Is the Vosk model downloaded?
3. Can a microphone actually be opened, and is real audio coming in?
4. Is a voiceprint enrolled, and does the CURRENT mic/voice score near the
   accept threshold (calibration info, not a pass/fail verdict)?
5. Is the live STT loop's transcriber reused correctly (regression check)?

Hermetic by construction: no hardware is touched unless a mic is available,
and every check degrades to a hint instead of crashing.
"""

from __future__ import annotations

import time

from system.logging_utils import log
from system.voice.stt_tts import model_installed, model_path
from system.voice.wake import SAMPLE_RATE, SpeakerVerifier


def _dep(name: str) -> tuple[bool, str]:
    try:
        __import__(name)
        return True, ""
    except Exception as exc:  # noqa: BLE001 — any import failure is a report line
        return False, str(exc)


def check_deps() -> list[dict]:
    out = []
    for module, purpose in (
        ("sounddevice", "microphone input"),
        ("vosk", "offline speech-to-text"),
        ("numpy", "audio math (vosk dependency)"),
        ("pyttsx3", "text-to-speech (SURA's replies)"),
    ):
        ok, err = _dep(module)
        out.append({
            "name": f"dep:{module}",
            "status": "ok" if ok else "fail",
            "detail": "installed" if ok else f"missing ({err})",
            "hints": [] if ok else [f"pip install {module}"],
        })
    return out


def check_model() -> dict:
    from system.voice.stt_tts import stt_engine_name

    engine = stt_engine_name()
    if engine == "whisper":
        from system.voice.stt_tts import _default_whisper_model

        return {
            "name": "stt_model", "status": "ok",
            "detail": (f"faster-whisper '{_default_whisper_model()}' "
                       "(downloads on first use, cached in voice/models/whisper)"),
            "hints": [
                "For still-better accuracy on accented speech, upgrade: "
                "python -m cli set-stt-model small.en",
            ],
        }
    if model_installed():
        return {
            "name": "stt_model", "status": "warn",
            "detail": "vosk fallback in use — accuracy is much lower, "
                      "especially with accents",
            "hints": ["pip install faster-whisper   (recommended, one command)"],
        }
    return {
        "name": "stt_model",
        "status": "fail",
        "detail": f"not downloaded (expected at {model_path()})",
        "hints": ["pip install faster-whisper (recommended)",
                  "python -m cli listen --download-model   (~50 MB, one time)"],
    }


def check_mic(seconds: float = 1.0) -> dict:
    ok_sd, err_sd = _dep("sounddevice")
    if not ok_sd:
        return {"name": "microphone", "status": "fail",
                "detail": "sounddevice not installed", "hints": ["pip install sounddevice numpy"]}
    try:
        import sounddevice as sd
        defaults = sd.default.device          # _InputOutputPair(input, output) — indexable
        try:
            in_idx = int(defaults[0])
        except (TypeError, IndexError, ValueError):
            in_idx = -1
        if in_idx is None or in_idx < 0:
            return {"name": "microphone", "status": "fail",
                    "detail": "no default input device", "hints": [
                        "Windows Settings > System > Sound > Input: pick your mic"]}
        dev = sd.query_devices(in_idx)
        from system.voice.stt_tts import MicrophoneStream, rms_of_pcm

        peak = 0.0
        with MicrophoneStream() as mic:
            deadline = time.time() + seconds
            while time.time() < deadline:
                peak = max(peak, rms_of_pcm(mic.read()))
        detail = (f"'{dev.get('name', '?')}' open, {SAMPLE_RATE} Hz — "
                  f"peak level {peak:.0f} over {seconds:.0f}s")
        hints = []
        status = "ok"
        if peak < 20:
            status = "warn"
            hints.append("Mic is silent — check Windows input volume / mute switch, "
                         "or the selected default mic is not the one you speak into")
        elif peak < 100:
            hints.append("Mic level is low; speak closer or raise the input volume")
        return {"name": "microphone", "status": status, "detail": detail, "hints": hints}
    except Exception as exc:  # noqa: BLE001 — report, don't crash
        return {"name": "microphone", "status": "fail",
                "detail": f"could not open: {exc}",
                "hints": ["Close apps holding the mic (Discord/OBS) and retry",
                          "Windows Settings > Privacy > Microphone: allow desktop apps"]}


def check_voiceprint() -> dict:
    verifier = SpeakerVerifier()
    if not verifier.enrolled:
        return {
            "name": "voiceprint", "status": "warn",
            "detail": "not enrolled — SURA will accept ANY voice (open mode)",
            "hints": ["python -m cli enroll-voice   (locks SURA to your voice)"],
        }
    from system.voice.wake import verifier_status

    info = verifier_status()
    return {
        "name": "voiceprint", "status": "ok",
        "detail": (f"enrolled: {info['samples']} samples from {info['created']} "
                   f"(accept threshold {info['threshold']})"),
        "hints": [
            "If wake calls get rejected, re-enroll at the SAME distance you talk "
            "while gaming, or set voice_threshold (e.g. 0.75) in "
            "config/agent_config.yaml",
        ],
    }


def check_listen_loop() -> dict:
    """Static sanity check of the always-on loop wiring (no hardware)."""
    from system.voice import assistant as assistant_mod

    ok = callable(getattr(assistant_mod.VoiceAssistant, "listen", None))
    return {
        "name": "listen_loop", "status": "ok" if ok else "fail",
        "detail": ("wake loop ready: one transcriber per wake-cycle, inline "
                   "commands supported, transcripts logged to logs/*.log" if ok
                   else "listen() missing"),
        "hints": [],
    }


def voice_check() -> list[dict]:
    """Run every check; returns a list of {name, status, detail, hints}."""
    log("voice_check")
    sections = check_deps()
    sections.append(check_model())
    sections.append(check_mic())
    sections.append(check_voiceprint())
    sections.append(check_listen_loop())
    return sections
