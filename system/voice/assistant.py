"""VoiceAssistant: the always-listening loop.

Cycle:
    dormant (wake listening) -> wake heard -> owner verified?
        -> yes: capture command -> orchestrator -> spoken reply
        -> no:  polite refusal (log it) -> back to dormant
        -> silence: back to dormant

Everything is injectable: mic, transcriber, speaker, verifier, and the
chat function are constructor parameters, so the full loop is unit-tested
with fakes and no audio hardware.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from system.logging_utils import log
from system.voice.stt_tts import rms_of_pcm
from system.voice.wake import (
    ENERGY_FLOOR,
    SAMPLE_RATE,
    SpeakerVerifier,
    VoiceError,
    is_wake,
    strip_wake,
)


@dataclass
class VoiceEvents:
    """Observer hooks for the CLI/UI to display what SURA is doing."""

    on_state: callable = print
    on_wake: callable = print
    on_verified: callable = print
    on_rejected: callable = print
    on_transcript: callable = print
    on_reply: callable = print


@dataclass
class LoopConfig:
    max_command_seconds: float = 12.0
    silence_after_speech: float = 1.6      # endpointing: stop after this much silence
    wake_reject_cooldown: float = 2.0      # pause after refusing a stranger
    wake_poll_log_seconds: float = 30.0    # periodic "still listening" event


class VoiceAssistant:
    def __init__(
        self,
        mic=None,                 # context manager with .read() -> pcm bytes
        transcriber_factory=None, # callable -> object with feed/finish
        speaker=None,             # object with .say(text)
        verifier: SpeakerVerifier | None = None,
        orchestrator=None,
        events: VoiceEvents | None = None,
        config: LoopConfig | None = None,
    ) -> None:
        self.mic = mic
        self.transcriber_factory = transcriber_factory
        self.speaker = speaker
        self.verifier = verifier or SpeakerVerifier()
        self.orchestrator = orchestrator  # lazy default (see _get_orchestrator)
        self.events = events or VoiceEvents()
        self.config = config or LoopConfig()
        self._orchestrator_instance = orchestrator

    def _get_orchestrator(self):
        """Build the real orchestrator on first use.

        The import lives here (not at module scope) so voice mode stays light,
        and it must be visible to this method: an earlier version imported it
        inside __init__ only, so `python -m cli listen` — which injects no
        orchestrator — raised NameError on the very first real command and the
        whole voice loop died.
        """
        if self._orchestrator_instance is None:
            from system.orchestrator import Orchestrator

            self._orchestrator_instance = Orchestrator()
        return self._orchestrator_instance

    # ------------------------------------------------------------ public API

    def handle_text_command(self, text: str) -> str:
        """Route a (non-wake) command through the orchestrator, like chat."""
        result = self._get_orchestrator().chat(text)
        return result.get("reply", "")

    def speak(self, text: str) -> None:
        if self.speaker:
            self.speaker.say(text)
        self.events.on_reply(text)

    def ack(self) -> None:
        """Short chirp when the wake word lands and the owner is verified.

        Without it there is no way to tell "SURA heard me but is still
        thinking" from "SURA never heard me" — the single most confusing
        part of using wake-word mode.
        """
        log("wake_ack")
        self._chirp()

    @staticmethod
    def _chirp() -> None:
        """The physical sound (a seam: tests replace it with silence)."""
        try:
            import winsound

            winsound.Beep(880, 90)
        except Exception:  # noqa: BLE001 — a missing chirp must never matter
            pass

    def _drain(self, mic, seconds: float = 0.8) -> None:
        """Discard audio captured while SURA was thinking or speaking.

        TTS runs to completion on this thread, so the mic keeps filling up
        behind it. Feeding that backlog to the next transcriber would mean
        acting on the tail of SURA's own voice (or stale room noise).
        """
        deadline = time.time() + seconds
        while time.time() < deadline:
            try:
                mic.read()
            except Exception:  # noqa: BLE001 — draining is best effort
                return

    def run_once(self, pcm_chunks: list[bytes]) -> str:
        """Deterministic single pass over pre-captured chunks (used by tests
        and by --once mode). Returns what happened."""
        transcriber = self.transcriber_factory()
        transcript_parts = []
        for chunk in pcm_chunks:
            text = transcriber.feed(chunk)
            if text:
                transcript_parts.append(text)
        transcript = " ".join(transcript_parts).strip() or transcriber.finish()

        if not transcript:
            return "silence"
        if not is_wake(transcript):
            return f"ignored: {transcript}"

        command = strip_wake(transcript)
        if not command:
            self.speak("I'm listening — say a command after the wake word.")
            return "wake only"

        if self.verifier.enrolled:
            utterance = b"".join(pcm_chunks)
            samples = self._pcm_to_float(utterance)
            try:
                ok, score = self.verifier.verify(samples)
            except VoiceError as exc:
                return f"verify error: {exc}"
            if not ok:
                self.events.on_rejected(score)
                log("voice_rejected", score=score, transcript=transcript)
                self.speak("Voice not recognized. This assistant answers only to Subhra.")
                return f"rejected (score={score})"
            self.events.on_verified(score)

        reply = self.handle_text_command(command)
        self.speak(reply)
        log("voice_command", command=command, reply_chars=len(reply))
        return f"executed: {command}"

    def listen(self) -> None:
        """The always-on loop. Ctrl+C to stop.

        One STT transcriber per wake-cycle keeps Vosk's audio buffer and the
        mic in lockstep: a recognizer that is re-created every couple of
        seconds drops whatever audio was buffered but not yet endpointed,
        which is exactly how wake words get lost in the real loop.
        """
        if self.mic is None or self.transcriber_factory is None:
            from system.voice.stt_tts import MicrophoneStream, Transcriber

            self.mic = MicrophoneStream()
            self.transcriber_factory = Transcriber
        if self.speaker is None:
            # Replies must be audible in real voice mode. `cli listen` injects
            # no speaker, and without one SURA answered only in the log — the
            # loop worked while seeming completely dead. TTS stays optional:
            # if it cannot start, voice input still works.
            try:
                from system.voice.stt_tts import Speaker

                self.speaker = Speaker()
            except Exception as exc:  # noqa: BLE001 — input matters more than output
                log("tts_unavailable", error=f"{type(exc).__name__}: {exc}")
                self.events.on_state(f"spoken replies unavailable: {exc}")

        cfg = self.config
        mic_context = None
        try:
            try:
                mic_context = self.mic
                mic = mic_context.__enter__()
            except Exception as exc:  # noqa: BLE001 — a busy/missing mic must not crash the loop
                log("mic_unavailable", error=f"{type(exc).__name__}: {exc}")
                self.events.on_state(f"microphone unavailable: {exc}")
                mic_context = None      # nothing opened, nothing to close
                return
            try:
                from system.voice.stt_tts import stt_engine_name

                self.events.on_state(
                    f"dormant — listening for the wake word (STT: {stt_engine_name()})"
                )
                while True:
                    last_log = time.time()
                    try:
                        transcriber = self.transcriber_factory()
                    except Exception as exc:  # noqa: BLE001 — missing model/deps must not crash
                        self.events.on_state(f"STT unavailable: {exc}")
                        return
                    transcript = self._await_wake(mic, transcriber, last_log)
                    if transcript is None:
                        continue

                    # ---- owner verification gate
                    if self.verifier.enrolled:
                        samples = self._pcm_to_float(self._wake_pcm)
                        try:
                            ok, score = self.verifier.verify(samples)
                        except VoiceError as exc:
                            self.events.on_state(f"verify error: {exc}")
                            continue
                        if not ok:
                            self.events.on_rejected(score)
                            self.speak("Voice not recognized. This assistant answers only to Subhra.")
                            log("voice_rejected", score=score, transcript=transcript)
                            self._drain(mic)
                            time.sleep(cfg.wake_reject_cooldown)
                            continue
                        self.events.on_verified(score)
                    self.ack()

                    # ---- command: inline text after the wake word first, else listen
                    command = strip_wake(transcript)
                    if command:
                        log("stt_final", transcript=transcript, phase="wake+command")
                    else:
                        command = self._capture_command(mic, transcriber)
                    if not command:
                        self.speak("I'm listening — say a command after the wake word.")
                        self._drain(mic)
                        continue

                    self.events.on_transcript(command)
                    try:
                        reply = self.handle_text_command(command)
                    except Exception as exc:  # noqa: BLE001 — one bad command must not end voice mode
                        log("voice_command_error", command=command[:120],
                            error=f"{type(exc).__name__}: {exc}")
                        self.events.on_state(f"command failed: {exc}")
                        self.speak("Something went wrong handling that. Still listening.")
                        self._drain(mic)
                        continue
                    self.speak(reply)
                    self._drain(mic)
                    log("voice_command", command=command[:120], reply_chars=len(reply))
            except Exception as exc:  # noqa: BLE001 — one surprise must not end voice mode
                log("voice_loop_error", error=f"{type(exc).__name__}: {exc}")
                self.events.on_state(f"voice loop stopped: {exc}")
        except KeyboardInterrupt:
            self.events.on_state("standing by (stopped)")
        finally:
            if mic_context is not None:
                try:
                    mic_context.__exit__(None, None, None)
                except Exception:  # noqa: BLE001 — closing is best effort
                    pass

    # ------------------------------------------------------------ internals

    def _await_wake(self, mic, transcriber, last_wake_log: float) -> str | None:
        """Feed mic audio until a final transcript contains the wake word.

        Every final result is logged so missed wakes are debuggable from
        logs/*.log — you can see exactly what STT thought it heard.
        """
        pcm_parts = []
        last_log = time.time()
        if time.time() - last_wake_log > self.config.wake_poll_log_seconds:
            log("wake_listening", state="dormant")
        while True:
            chunk = mic.read()
            if time.time() - last_log > self.config.wake_poll_log_seconds:
                log("wake_listening", state="dormant")
                last_log = time.time()
            pcm_parts.append(chunk)
            text = transcriber.feed(chunk)
            if text:
                log("stt_final", transcript=text, phase="wake_listen")
                if is_wake(text):
                    self._wake_pcm = b"".join(pcm_parts)
                    self.events.on_wake(text)
                    return text
        # Not reachable: feed() only returns finals; the loop above runs until
        # a final contains the wake word (the caller re-creates transcribers).

    def _capture_command(self, mic, transcriber) -> str:
        """Record until silence endpoint or the max duration; return the command text."""
        cfg = self.config
        start = time.time()
        last_sound = time.time()
        speech_started = False
        transcript = ""
        while time.time() - start < cfg.max_command_seconds:
            chunk = mic.read()
            energy = rms_of_pcm(chunk)
            if energy > ENERGY_FLOOR:
                speech_started = True
                last_sound = time.time()
            elif speech_started and time.time() - last_sound > cfg.silence_after_speech:
                break
            text = transcriber.feed(chunk)
            if text:
                transcript = text.strip()
                log("stt_final", transcript=transcript, phase="command")
                break
        if not transcript:
            transcript = transcriber.finish().strip()
        return transcript

    def _pcm_to_float(self, pcm: bytes) -> list[float]:
        from system.voice.stt_tts import pcm_to_float

        return pcm_to_float(pcm)


def build_assistant(events: VoiceEvents | None = None) -> VoiceAssistant:
    """Real-hardware assistant factory (used by `python -m cli listen`)."""
    from system.voice.stt_tts import MicrophoneStream, Transcriber

    return VoiceAssistant(
        mic=MicrophoneStream(),
        transcriber_factory=Transcriber,
        events=events,
    )
