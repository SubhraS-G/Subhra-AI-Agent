"""Hands-free voiceprint recapture v2 — continuous listen + auto-segmentation.

Run once, then simply say the enrollment phrase 7 times with 1-2 s pauses.
No key presses, no timing skill needed: the script records continuously,
splits utterances on silence, enrolls the best 6, and calibrates from the
7th (or from cross-sample similarity when fewer are heard).
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from system.voice.stt_tts import MicrophoneStream, SpeechError, pcm_to_float, rms_of_pcm
from system.voice.wake import SAMPLE_RATE, SpeakerVerifier

PHRASE = "Hey SURA, run my thumbnail workflow."
TOTAL_S = 45.0
SPEECH_RMS = 200          # slightly sensitive: this mic array runs quiet
SILENCE_S = 0.7
MIN_DUR = 0.6
MAX_DUR = 8.0
LEAD_IN = 0.25            # audio kept before the first loud chunk


def record_window(mic, seconds: float) -> list[float]:
    """Continuously record `seconds` of audio as a float list."""
    chunks = []
    deadline = time.time() + seconds
    while time.time() < deadline:
        chunks.append(mic.read())
    return pcm_to_float(b"".join(chunks))


def segment(samples: list[float]) -> list[list[float]]:
    """Split a continuous float stream into utterances on silence gaps."""
    utterances: list[list[float]] = []
    current: list[float] = []
    quiet_for = 0.0
    chunk_s = 0.1                       # mic.read() yields ~100 ms chunks
    lead = int(LEAD_IN / chunk_s)
    for i in range(0, len(samples) - 1, int(chunk_s * SAMPLE_RATE)):
        window = samples[i:i + int(chunk_s * SAMPLE_RATE)]
        if not window:
            continue
        peak = max((abs(s) for s in window), default=0.0)
        loud = peak * 32767 > SPEECH_RMS
        if loud:
            if not current:
                start = max(0, i - lead * int(chunk_s * SAMPLE_RATE))
                current = samples[start:i]
            current.extend(window)
            quiet_for = 0.0
        elif current:
            current.extend(window)      # keep trailing audio in case speech resumes
            quiet_for += chunk_s
            if quiet_for > SILENCE_S:
                dur = len(current) / SAMPLE_RATE
                if MIN_DUR <= dur <= MAX_DUR:
                    utterances.append(current)
                current = []
                quiet_for = 0.0
    if current and MIN_DUR <= len(current) / SAMPLE_RATE <= MAX_DUR:
        utterances.append(current)
    return utterances


def main() -> int:
    verifier = SpeakerVerifier()
    print(f"RECSTEP record {TOTAL_S:.0f}s — say `{PHRASE}` 7 times, 1-2s pauses.")
    try:
        with MicrophoneStream() as mic:
            samples = record_window(mic, TOTAL_S)
    except SpeechError as exc:
        print(f"SETUP NEEDED: {exc}")
        return 1
    utterances = segment(samples)
    for i, utt in enumerate(utterances):
        peak = max((abs(s) for s in utt), default=0.0)
        print(f"RECSTEP utterance {i + 1}: {len(utt) / SAMPLE_RATE:.1f}s peak={peak:.2f}")
    if len(utterances) < 3:
        print(f"FAILED: only {len(utterances)} utterances heard (need >=3). "
              "Speak louder / closer and re-run.")
        return 1
    cal = None
    if len(utterances) >= 7:
        cal = utterances[6]
        enroll_list = utterances[:6]
    else:
        enroll_list = utterances
    verifier.enroll(enroll_list, owner="subhra")
    print(f"RECSTEP enrolled: {len(enroll_list)} samples")
    if cal is not None:
        ok, score = verifier.verify(cal)
        print(f"RECSTEP calibration live score: {score} (accepted={ok}, "
              f"old threshold={verifier.threshold})")
        suggested = round(max(0.55, min(0.85, score - 0.08)), 2)
    else:
        from system.voice.wake import cosine_similarity
        print("RECSTEP no 7th utterance; calibrating from cross-sample similarity:")
        import system.voice.wake as wk
        mfc = [wk._mfcc_like(wk._normalize(u)) for u in enroll_list]
        scores = []
        for i in range(len(mfc)):
            for j in range(i + 1, len(mfc)):
                w = min(len(mfc[i]), len(mfc[j]))
                scores.append(cosine_similarity(mfc[i][:w], mfc[j][:w]))
        mean = sum(scores) / len(scores) if scores else 0.7
        print(f"RECSTEP mean pairwise similarity: {mean:.3f} across {len(scores)} pairs")
        suggested = round(max(0.55, min(0.85, mean - 0.08)), 2)
    print(f"SUGGESTED_THRESHOLD={suggested}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
