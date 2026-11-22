"""Voice layer tests — fully hermetic (synthetic audio, fake transcribers)."""

from __future__ import annotations

import math
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from system.voice.assistant import VoiceAssistant, VoiceEvents
from system.voice.wake import (
    SAMPLE_RATE,
    SpeakerVerifier,
    VoiceError,
    cosine_similarity,
    is_wake,
    strip_wake,
)


def synth_voice(seed_freq: float, seconds: float = 1.2, noise: float = 0.0) -> list[float]:
    """A synthetic 'voice': harmonic stack with a speaker-specific pitch."""
    n = int(SAMPLE_RATE * seconds)
    out = []
    for i in range(n):
        t = i / SAMPLE_RATE
        value = (
            0.5 * math.sin(2 * math.pi * seed_freq * t)
            + 0.25 * math.sin(2 * math.pi * seed_freq * 2 * t)
            + 0.12 * math.sin(2 * math.pi * seed_freq * 3 * t)
        )
        if noise:
            value += noise * math.sin(2 * math.pi * 977 * t)
        out.append(value / 1.0)
    return out


class TestWake(unittest.TestCase):
    def test_exact_and_variants(self):
        self.assertTrue(is_wake("hey sura"))
        self.assertTrue(is_wake("SURA what's up"))
        self.assertTrue(is_wake("soora open the workflow"))      # STT misspelling
        self.assertTrue(is_wake("hey surah make a thumbnail"))

    def test_non_wake_ignored(self):
        self.assertFalse(is_wake("what time is it"))
        self.assertFalse(is_wake(""))
        self.assertFalse(is_wake("samsung phone review"))

    def test_greeting_plus_mishearing_wakes(self):
        # Measured mishearing: TTS "hey sura what is the status" -> whisper
        # "Hey, sir, what is the status?" — this is what silenced the wake word.
        self.assertTrue(is_wake("Hey, sir, what is the status?"))
        self.assertTrue(is_wake("hey sara open spotify"))
        self.assertTrue(is_wake("OK Sarah, make a thumbnail"))

    def test_observed_whisper_mishearings_wake(self):
        # All four were really produced by whisper (base.en) on this machine
        # when the owner said "hey SURA".
        self.assertTrue(is_wake("Hey, so, what's the status?"))
        self.assertTrue(is_wake("Hey, so Rama, what's the status?"))
        self.assertTrue(is_wake("Hey, soo."))
        self.assertEqual(strip_wake("Hey, so, what's the status?"), "whats the status")

    def test_bare_sir_does_not_wake(self):
        # A lone "sir" mid-sentence (videos, other people) must stay asleep.
        self.assertFalse(is_wake("the report is ready sir"))
        self.assertFalse(is_wake("sir"))
        self.assertFalse(is_wake("make sure you open it later"))

    def test_strip_wake(self):
        self.assertEqual(strip_wake("hey sura make a thumbnail for valorant"),
                         "make a thumbnail for valorant")
        self.assertEqual(strip_wake("sura"), "")
        self.assertEqual(strip_wake("Hey, sir, what is the status?"),
                         "what is the status")
        self.assertEqual(strip_wake("hey sara open spotify"), "open spotify")
        # Repeating the wake phrase (what people do when SURA seems asleep)
        # must not leave "hey sir" behind to be sent as the command.
        self.assertEqual(strip_wake("Hey, sir. Hey, sir"), "")
        self.assertEqual(strip_wake("sura sura open spotify"), "open spotify")


class TestSpeakerVerification(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="voice-test-"))

    def _verifier(self, threshold: float = 0.90) -> SpeakerVerifier:
        return SpeakerVerifier(path=self.dir / "vp.json", threshold=threshold)

    def test_enroll_and_accept_owner(self):
        verifier = self._verifier()
        verifier.enroll([
            synth_voice(120), synth_voice(122), synth_voice(119),
        ])
        ok, score = verifier.verify(synth_voice(121))
        self.assertTrue(ok, f"owner rejected with score={score}")

    def test_reject_different_voice(self):
        verifier = self._verifier()
        verifier.enroll([synth_voice(120), synth_voice(122), synth_voice(119)])
        ok, score = verifier.verify(synth_voice(260))   # much higher pitch
        self.assertFalse(ok, f"stranger accepted with score={score}")

    def test_reject_noise(self):
        verifier = self._verifier()
        verifier.enroll([synth_voice(120), synth_voice(122), synth_voice(119)])
        ok, _ = verifier.verify(synth_voice(120, noise=0.9))
        self.assertFalse(ok)

    def test_fail_closed_without_enrollment(self):
        with self.assertRaises(VoiceError):
            self._verifier().verify(synth_voice(120))

    def test_persists_to_disk(self):
        verifier = self._verifier()
        verifier.enroll([synth_voice(120), synth_voice(121)])
        again = self._verifier()
        self.assertTrue(again.enrolled)
        ok, _ = again.verify(synth_voice(120))
        self.assertTrue(ok)


class _FakeTranscriber:
    """Scripted transcriber: yields queued final texts in order."""

    def __init__(self, scripts: list[str]) -> None:
        self.scripts = list(scripts)
        self.index = 0

    def feed(self, chunk: bytes) -> str | None:
        if self.index < len(self.scripts):
            text = self.scripts[self.index]
            self.index += 1
            return text
        return None

    def finish(self) -> str:
        if self.index < len(self.scripts):
            text = self.scripts[self.index]
            self.index += 1
            return text
        return ""


def pcm_chunks_for(samples: list[float], chunk_count: int = 3):
    """Encode synthetic samples as int16 PCM chunks (the 'fake mic')."""
    from system.voice.stt_tts import float_to_pcm

    pcm = float_to_pcm(samples)
    size = len(pcm) // chunk_count
    return [pcm[i * size : (i + 1) * size] for i in range(chunk_count)]


class _FakeSpeaker:
    def __init__(self) -> None:
        self.spoken: list[str] = []

    def say(self, text: str) -> None:
        self.spoken.append(text)


class TestVoiceLoop(unittest.TestCase):
    def _assistant(self, scripts: list[str], enrolled: bool = True, threshold: float = 0.90):
        dir_path = Path(tempfile.mkdtemp(prefix="voice-loop-"))
        verifier = SpeakerVerifier(path=dir_path / "vp.json", threshold=threshold)
        if enrolled:
            verifier.enroll([synth_voice(120), synth_voice(122), synth_voice(119)])
        speaker = _FakeSpeaker()
        replies = []

        class FakeOrchestrator:
            def chat(self, message, history=None):
                reply = f"done: {message}"
                replies.append((message, reply))
                return {"reply": reply, "mode": "rule", "tool_trace": []}

        events = VoiceEvents(
            on_state=lambda s: None, on_wake=lambda t: None,
            on_verified=lambda s: None, on_rejected=lambda s: None,
            on_transcript=lambda t: None, on_reply=lambda t: None,
        )
        assistant = VoiceAssistant(
            mic=object(),  # not used by run_once
            transcriber_factory=lambda: _FakeTranscriber(scripts),
            speaker=speaker,
            verifier=verifier,
            orchestrator=FakeOrchestrator(),
            events=events,
        )
        return assistant, speaker, replies

    def test_wake_then_command_executes(self):
        assistant, speaker, replies = self._assistant(["hey sura make a thumbnail for valorant"])
        chunks = pcm_chunks_for(synth_voice(120, 1.2))
        outcome = assistant.run_once(chunks)
        self.assertTrue(outcome.startswith("executed"), outcome)
        self.assertEqual(replies[0][0], "make a thumbnail for valorant")
        self.assertEqual(speaker.spoken, [replies[0][1]])

    def test_stranger_rejected_before_execution(self):
        # 'voice' 260 Hz != owner 120 Hz -> verification must fail.
        dir_path = Path(tempfile.mkdtemp(prefix="voice-loop-"))
        verifier = SpeakerVerifier(path=dir_path / "vp.json", threshold=0.90)
        verifier.enroll([synth_voice(120), synth_voice(122), synth_voice(119)])
        speaker = _FakeSpeaker()
        assistant = VoiceAssistant(
            mic=object(),
            transcriber_factory=lambda: _FakeTranscriber(["hey sura delete everything"]),
            speaker=speaker,
            verifier=verifier,
            orchestrator=None,  # must never be reached
            events=VoiceEvents(on_state=lambda s: None, on_wake=lambda t: None,
                               on_verified=lambda s: None, on_rejected=lambda s: None,
                               on_transcript=lambda t: None, on_reply=lambda t: None),
        )
        # Patch _pcm_to_float to return the stranger's voice.
        assistant._pcm_to_float = lambda pcm: synth_voice(260)
        outcome = assistant.run_once([b"\x00\x00" * 400])
        self.assertTrue(outcome.startswith("rejected"), outcome)
        self.assertEqual(speaker.spoken, ["Voice not recognized. This assistant answers only to Subhra."])

    def test_wake_only_gets_prompt(self):
        assistant, speaker, replies = self._assistant(["hey sura"])
        outcome = assistant.run_once(pcm_chunks_for(synth_voice(120, 1.0)))
        self.assertEqual(outcome, "wake only")
        self.assertIn("listening", speaker.spoken[0].lower())

    def test_non_wake_ignored(self):
        assistant, speaker, replies = self._assistant(["what time is it"])
        outcome = assistant.run_once(pcm_chunks_for(synth_voice(120, 1.0)))
        self.assertTrue(outcome.startswith("ignored"), outcome)
        self.assertEqual(replies, [])

    def test_unenrolled_system_still_executes_text_commands(self):
        # No voiceprint: verification is skipped (open mode) — user's choice.
        assistant, speaker, replies = self._assistant(["hey sura run status"], enrolled=False)
        outcome = assistant.run_once(pcm_chunks_for(synth_voice(120, 1.0)))
        self.assertTrue(outcome.startswith("executed"), outcome)

    def test_listen_creates_a_speaker_so_replies_are_audible(self):
        """Regression: `cli listen` passed no speaker, so every spoken reply
        silently stayed in the log while the loop looked dead."""
        from system.voice import stt_tts as stt_module

        spoken: list[str] = []

        class FakeTTS:
            def say(self, text: str) -> None:
                spoken.append(text)

        state = {"fired": False}

        class WakeOnce:
            def feed(self, chunk):
                if not state["fired"]:
                    state["fired"] = True
                    return "hey sura status"
                return None

            def finish(self):
                return ""

        class Mic:
            def __init__(self):
                self.reads = 0

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self):
                self.reads += 1
                if self.reads > 12:
                    raise KeyboardInterrupt   # Ctrl+C ends the loop, like a real user
                return b"\x00\x00" * 800

        class FakeOrchestrator:
            def chat(self, message, history=None):
                return {"reply": f"done: {message}", "mode": "rule", "tool_trace": []}

        events = VoiceEvents(
            on_state=lambda s: None, on_wake=lambda t: None,
            on_verified=lambda s: None, on_rejected=lambda s: None,
            on_transcript=lambda t: None, on_reply=lambda t: None,
        )
        verifier = SpeakerVerifier(path=Path(tempfile.mkdtemp()) / "vp.json")
        with mock.patch.object(stt_module, "Speaker", FakeTTS):
            assistant = VoiceAssistant(mic=Mic(), transcriber_factory=WakeOnce,
                                       speaker=None, verifier=verifier,
                                       orchestrator=FakeOrchestrator(), events=events)
            assistant._chirp = lambda: None          # keep the test suite silent
            assistant.listen()
        self.assertEqual(spoken, ["done: status"])   # heard through the new speaker

    def test_lazy_orchestrator_is_reachable(self):
        """`cli listen` injects no orchestrator; the lazy path must work.

        Regression: the import lived inside __init__, so the first real
        command raised NameError and killed the whole voice loop.
        """
        from system import orchestrator as orchestrator_module

        class FakeOrchestrator:
            def chat(self, message, history=None):
                return {"reply": f"lazy: {message}", "mode": "rule", "tool_trace": []}

        verifier = SpeakerVerifier(path=Path(tempfile.mkdtemp()) / "vp.json")
        with mock.patch.object(orchestrator_module, "Orchestrator", FakeOrchestrator):
            assistant = VoiceAssistant(mic=None, transcriber_factory=None,
                                       speaker=_FakeSpeaker(), verifier=verifier,
                                       orchestrator=None)
            self.assertEqual(assistant.handle_text_command("status please"),
                             "lazy: status please")

    def test_a_failed_command_does_not_end_voice_mode(self):
        """One exploding command used to kill the listener; it must recover."""
        verifier = SpeakerVerifier(path=Path(tempfile.mkdtemp()) / "vp.json")  # open mode
        speaker = _FakeSpeaker()

        class OneShotMic:
            def __init__(self):
                self.reads = 0

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self):
                self.reads += 1
                if self.reads > 6:
                    raise KeyboardInterrupt      # ends the test like a real Ctrl+C
                return b"\x00\x00" * 800

        wake_state = {"fired": False}      # the loop builds a new transcriber per cycle

        class WakeOnceTranscriber:
            def feed(self, chunk):
                if not wake_state["fired"]:
                    wake_state["fired"] = True
                    return "hey sura boom"       # wake + inline command, once
                return None

            def finish(self):
                return ""

        events = VoiceEvents(
            on_state=lambda s: None, on_wake=lambda t: None,
            on_verified=lambda s: None, on_rejected=lambda s: None,
            on_transcript=lambda t: None, on_reply=lambda t: None,
        )
        assistant = VoiceAssistant(mic=OneShotMic(),
                                   transcriber_factory=WakeOnceTranscriber,
                                   speaker=speaker, verifier=verifier, events=events)

        def exploding_command(message):
            raise RuntimeError("brain exploded")

        assistant.handle_text_command = exploding_command
        assistant._chirp = lambda: None
        assistant.listen()                        # must return, not raise
        self.assertEqual(speaker.spoken,
                         ["Something went wrong handling that. Still listening."])
        self.assertEqual(speaker.spoken,
                         ["Something went wrong handling that. Still listening."])


class TestTranscriberResilience(unittest.TestCase):
    """Whisper decoding must never kill the voice loop, and must never
    hand raw bytes to PyAV (that decoder blows up on some PyAV builds)."""

    def _transcriber(self, model):
        import time as _time

        from system.voice import stt_tts as mod

        with mock.patch.object(mod.Transcriber, "_make_whisper", return_value=model), \
             mock.patch.object(mod, "model_installed", return_value=True), \
             mock.patch.object(mod.Transcriber, "_make_vosk",
                               return_value=FakeVosk()):
            transcriber = mod.Transcriber()
        # Pretend we just heard speech and the room has gone quiet — that is
        # exactly the state that triggers a decode.
        transcriber._buffer = b"\x00\x00" * 8000
        transcriber._speech_seen = True
        transcriber._started = True
        transcriber._last_sound = _time.monotonic() - 2.0
        return transcriber

    def test_decode_failure_falls_back_to_vosk_instead_of_dying(self):
        from system.voice import stt_tts as mod

        class BrokenWhisper:
            def transcribe(self, *args, **kwargs):
                raise TypeError("open() got an unexpected keyword argument 'metadata_errors'")

        transcriber = self._transcriber(BrokenWhisper())
        with mock.patch.object(mod, "model_installed", return_value=True), \
             mock.patch.object(mod.Transcriber, "_make_vosk", return_value=FakeVosk()):
            self.assertIsNone(transcriber.feed(b"\x00\x00" * 800))  # no exception
            self.assertEqual(transcriber._engine, "vosk")           # self-healed
            self.assertEqual(transcriber.feed(b"\x00\x00" * 800), "hey sura")

    def test_whisper_receives_an_array_not_bytes(self):
        import numpy as np

        seen = {}

        class FakeWhisper:
            def transcribe(self, audio, **kwargs):
                seen["audio"] = audio
                return [], None

        transcriber = self._transcriber(FakeWhisper())
        transcriber._decode_pcm(b"\x00\x00" * 1600)
        self.assertIsInstance(seen["audio"], np.ndarray)   # PyAV never runs
        self.assertEqual(seen["audio"].dtype, np.float32)


class TestSpeakerProcessIsolation(unittest.TestCase):
    """Speech only works once per process on this platform, so every
    utterance must be spoken by a fresh helper process."""

    def test_each_utterance_spawns_a_fresh_helper(self):
        from system.voice import stt_tts as mod

        calls = {}

        def fake_run(cmd, **kwargs):
            calls["cmd"] = cmd
            calls["env"] = kwargs.get("env", {})
            return type("Result", (), {"returncode": 0})()

        with mock.patch.object(mod.subprocess, "run", fake_run), \
             mock.patch.dict(os.environ, {}, clear=True):
            speaker = mod.Speaker()
            speaker.say("hello there")
        self.assertEqual(calls["cmd"][1:5], ["-m", "cli", "say", "hello there"])
        self.assertEqual(calls["env"].get(mod.INLINE_ENV), "1")

    def test_helper_process_speaks_inline_without_spawning(self):
        from system.voice import stt_tts as mod

        spoken = []
        with mock.patch.dict(os.environ, {mod.INLINE_ENV: "1"}), \
             mock.patch.object(mod.Speaker, "speak_inline",
                               lambda self, text: spoken.append(text)), \
             mock.patch.object(mod.subprocess, "run",
                               side_effect=AssertionError("must not spawn")):
            speaker = mod.Speaker()
            speaker.say("inline please")
        self.assertEqual(spoken, ["inline please"])


class FakeVosk:
    """Minimal stand-in for the Vosk recognizer."""

    def AcceptWaveform(self, chunk) -> bool:
        return True

    def Result(self) -> str:
        return '{"text": "hey sura"}'


if __name__ == "__main__":
    unittest.main()
