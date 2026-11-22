"""SURA — Subhra's Unified Reasoning Assistant. Command line interface.

Usage:
    python -m cli thumbnail "thumbnail for <video topic>" [--channel subhra-gaming] [options]
    python -m cli learn "<feedback>" [--scope thumbnails]
    python -m cli status
    python -m cli permissions

Examples:
    python -m cli thumbnail "Valorant ranked grind highlights" --tone excited
"""

from __future__ import annotations

import argparse
import json
import sys

from system.orchestrator import Orchestrator
from system.permissions import Tier


def _cmd_thumbnail(args: argparse.Namespace) -> int:
    context = {"agent": "creative", "video_topic": args.topic}
    if args.channel:
        context["channel_override"] = args.channel
    if args.tone:
        context["emotional_tone"] = args.tone
    if args.text:
        context["text_overlay"] = args.text
    if args.pattern:
        context["concept_pattern"] = args.pattern
    if args.render:
        context["render_path"] = args.render
    if args.no_face:
        context["creator_in_frame"] = False

    orchestrator = Orchestrator()
    result = orchestrator.request(args.objective, context)
    print(result.report())
    return 0 if result.status in {"completed", "needs_approval", "needs_input"} else 1


def _cmd_learn(args: argparse.Namespace) -> int:
    orchestrator = Orchestrator()
    print(orchestrator.learn(args.feedback, scope=args.scope))
    return 0


def _cmd_chat(args: argparse.Namespace) -> int:
    """Interactive conversation with the LLM brain (rule-mode notice if unconfigured)."""
    orchestrator = Orchestrator()
    history: list[dict] = []
    from system.paths import AGENT_NAME
    from system.llm.client import load_config

    cfg = load_config()
    if cfg.enabled:
        print(f"{AGENT_NAME} — chat · brain: {cfg.provider}/{cfg.model}")
    else:
        print(f"{AGENT_NAME} — chat · RULE MODE (no API key found)")
        print("  fix:  python -m cli set-key nvidia <your-key>   (persists forever)")
    print("  (type 'exit' to quit, 'reset' to clear context)")
    while True:
        try:
            user = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not user:
            continue
        if user.lower() in {"exit", "quit"}:
            return 0
        if user.lower() == "reset":
            history = []
            print("(context cleared)")
            continue
        result = orchestrator.chat(user, history=history)
        print(f"agent> {result['reply']}")
        if result.get("tool_trace"):
            for step in result["tool_trace"]:
                print(f"  [tool] {step['tool']}")
        # Keep the last 8 exchanges to bound context growth.
        history = (history + [
            {"role": "user", "content": user},
            {"role": "assistant", "content": result["reply"]},
        ])[-16:]


def _cmd_status(args: argparse.Namespace) -> int:
    from system.agents.creative.face_library import FaceLibrary
    from system.brands import BrandRegistry, load_brand_identity

    print("SUBHRA AI AGENT — STATUS")
    print()
    from system.llm.client import load_config

    cfg = load_config()
    if cfg.enabled:
        print(f"LLM brain: {cfg.provider} / {cfg.model}  (key: {cfg.api_key[:6]}…)")
    else:
        print("LLM brain: RULE MODE — no API key.  fix: python -m cli set-key nvidia <key>")
    print()
    faces = FaceLibrary().status()
    print(f"Face library: {faces['total']} references")
    for expression, count in sorted(faces["by_expression"].items()):
        print(f"  - {expression}: {count}")
    missing = faces["missing_expressions"]
    if missing:
        print(f"  (missing expressions: {', '.join(missing)})")
    print()
    print("Brands:")
    for brand in BrandRegistry().list_brands():
        identity = load_brand_identity(brand)
        state = "filled" if identity["filled"] else "unfilled (VISUAL_IDENTITY.md is a template)"
        print(f"  - {brand.name}: {state}")
    print()
    prefs = orchestrator_memory_preferences()
    if prefs:
        print(f"Learned preferences: {len(prefs)}")
        for pref in prefs:
            print(f"  - [{pref.confidence}] {pref.rule} (evidence={pref.evidence_count})")
    else:
        print("Learned preferences: none yet")
    return 0


def orchestrator_memory_preferences():
    from system.memory import MemoryStore

    return MemoryStore().list_preferences()


def _cmd_permissions(args: argparse.Namespace) -> int:
    from system.permissions import PermissionGate

    print(PermissionGate().describe())
    return 0


def _resolve_pairing_key(explicit: str) -> str:
    """Stable pairing key for shared mode: --key wins, else config, else new.

    The key is persisted to config/agent_config.yaml so the phone's stored
    key keeps working after reboots and across `serve`/`autostart` uses.
    """
    from system.llm.client import CONFIG_FILE, _parse_simple_yaml, _quote
    from system.server import generate_pairing_key, sanitize_key

    def persist(key: str) -> str:
        values = {}
        if CONFIG_FILE.exists():
            values = _parse_simple_yaml(CONFIG_FILE.read_text(encoding="utf-8"))
        values["pairing_key"] = key
        CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_FILE.write_text(
            "\n".join(f"{k}: {_quote(str(v))}" for k, v in values.items()) + "\n",
            encoding="utf-8",
        )
        return key

    key = sanitize_key(explicit)
    if key:
        return persist(key)
    if CONFIG_FILE.exists():
        stored = sanitize_key(str(
            _parse_simple_yaml(CONFIG_FILE.read_text(encoding="utf-8")).get("pairing_key", "")
        ))
        if stored:
            return stored
    return persist(generate_pairing_key())


def _cmd_serve(args: argparse.Namespace) -> int:
    from system.server import DEFAULT_PORT, serve

    share = bool(getattr(args, "share", False))
    tunnel = bool(getattr(args, "tunnel", False))
    pairing_key = ""
    if share or tunnel:
        pairing_key = _resolve_pairing_key(args.key or "")
    serve(
        port=args.port or DEFAULT_PORT,
        share=share or tunnel,
        pairing_key=pairing_key,
        qr=bool(getattr(args, "qr", False)),
        tunnel=tunnel,
    )
    return 0


def _cmd_supervise(args: argparse.Namespace) -> int:
    """Run the web UI under a watchdog — this is what the login launcher runs.

    Starts `serve` as a child and restarts it whenever it exits, so SURA is
    online the moment the app window opens and stays that way. Refuses to
    start a second server when one is already listening.
    """
    from system.server import DEFAULT_PORT
    from system.supervisor import supervise

    share = bool(args.share or args.tunnel)
    pairing_key = _resolve_pairing_key(args.key or "") if share else ""
    supervise(
        port=args.port or DEFAULT_PORT,
        share=share,
        pairing_key=pairing_key,
        tunnel=bool(args.tunnel),
        log_path=args.log or None,
        with_voice=bool(getattr(args, "with_voice", False)),
        voice_log_path=getattr(args, "voice_log", None) or None,
    )
    return 0


def _cmd_push(args: argparse.Namespace) -> int:
    """Drop a note into SURA's push inbox; paired devices show it instantly."""
    from system.server import push_message

    item = push_message(args.text, source="laptop")
    print(f"Pushed to every paired device: {item['text'][:120]}")
    print("(Devices see it on their next poll — the phone UI polls every few seconds.)")
    return 0


def _cmd_listen(args: argparse.Namespace) -> int:
    """SURA's always-on voice loop: wake word -> verify -> command -> spoken reply."""
    from system.voice.assistant import VoiceAssistant, VoiceEvents
    from system.voice.stt_tts import SpeechError, Transcriber, download_model, model_installed
    from system.voice.wake import SpeakerVerifier

    if args.download_model:
        if model_installed():
            print("Vosk model already installed.")
            return 0

        def show(pct):
            print(f"\rdownloading model… {pct}%", end="", flush=True)

        download_model(progress=show)
        print("\nmodel installed.")
        return 0

    from system.server import post_voice_turn

    def show_transcript(text: str) -> None:
        print(f"[you] {text}")
        post_voice_turn("you", text)      # mirror onto the chat screen

    def show_reply(text: str) -> None:
        print(f"[SURA] {text[:400]}")
        post_voice_turn("sura", text)

    try:
        assistant = VoiceAssistant(
            events=VoiceEvents(
                on_state=lambda s: print(f"[SURA] {s}"),
                on_wake=lambda t: print(f"[SURA] wake word heard ({t!r})"),
                on_verified=lambda s: print(f"[SURA] voice verified (score={s})"),
                on_rejected=lambda s: print(f"[SURA] UNKNOWN VOICE (score={s}) — ignoring"),
                on_transcript=show_transcript,
                on_reply=show_reply,
            ),
        )
    except SpeechError as exc:
        print(f"Setup needed: {exc}")
        return 1

    if not SpeakerVerifier().enrolled:
        print("[SURA] No voiceprint enrolled — ANYONE's voice will be accepted.")
        print("       Lock SURA to your voice only:  python -m cli enroll-voice")

    if not model_installed():
        try:
            from system.voice.stt_tts import stt_engine_name

            whisper_available = stt_engine_name() == "whisper"
        except Exception:  # noqa: BLE001
            whisper_available = False
        if whisper_available:
            print("[SURA] faster-whisper active — no legacy model needed "
                  "(the Whisper model downloads itself on first use).")
        else:
            print("[SURA] STT model missing. Either: pip install faster-whisper (recommended)")
            print("       or download the legacy fallback: python -m cli listen --download-model")
            return 1

    assistant.listen()
    return 0


def _cmd_enroll_voice(args: argparse.Namespace) -> int:
    """Build SURA's voiceprint of YOUR voice (3 samples)."""
    import time

    from system.voice.stt_tts import MicrophoneStream, SpeechError, pcm_to_float, rms_of_pcm
    from system.voice.wake import SAMPLE_RATE, SpeakerVerifier

    samples_needed = 3
    captured: list[list[float]] = []
    verifier = SpeakerVerifier()
    print(f"Enrolling {verifier.print_.owner if verifier.enrolled else 'your'} voice for SURA.")
    print(f"Speak the phrase below {samples_needed} times, naturally (mic: {SAMPLE_RATE} Hz).")
    print('Phrase:  "Hey SURA, run my thumbnail workflow."')
    try:
        with MicrophoneStream() as mic:
            for i in range(samples_needed):
                input(f"\n[{i + 1}/{samples_needed}] Press Enter, then speak… ")
                chunks = []
                deadline = time.time() + 4.0
                spoken = False
                while time.time() < deadline:
                    pcm = mic.read()
                    chunks.append(pcm)
                    if rms_of_pcm(pcm) > 300:
                        spoken = True
                    if spoken and time.time() > deadline - 1.0:
                        break
                audio = pcm_to_float(b"".join(chunks))
                captured.append(audio)
                print(f"     captured {len(audio) / SAMPLE_RATE:.1f}s (peak {max((abs(s) for s in audio), default=0):.2f})")
    except SpeechError as exc:
        print(f"Setup needed: {exc}")
        return 1
    except KeyboardInterrupt:
        print("\ncancelled.")
        return 1

    try:
        vp = verifier.enroll(captured)
    except Exception as exc:  # noqa: BLE001 — enrollment feedback, not a crash
        print(f"Enrollment failed: {exc}")
        return 1
    print(f"\nVoiceprint saved: {vp.sample_count} samples -> {verifier.path}")
    print("SURA now answers ONLY to this voice. Say 'hey sura' to test.")
    return 0


def _cmd_say(args: argparse.Namespace) -> int:
    """Speak text aloud through SURA's voice."""
    from system.voice.stt_tts import Speaker, SpeechError

    try:
        Speaker().say(args.text)
    except SpeechError as exc:
        print(f"Setup needed: {exc}")
        return 1
    return 0


def _cmd_set_key(args: argparse.Namespace) -> int:
    """Persist an API key so every new terminal / the UI / voice all see it.

    Also supports:  python -m cli set-key          (interactive, masked input)
                    python -m cli set-key --list   (show what's configured)
    """
    from getpass import getpass

    from system.llm.client import LLMError, load_config, save_key

    if args.list:
        cfg = load_config()
        if not cfg.enabled:
            print("No LLM configured (rule mode). Set a key:")
            print("  python -m cli set-key nvidia <key>    <- your NVIDIA NIM key")
            return 0
        masked = cfg.api_key[:6] + "…" + cfg.api_key[-4:] if len(cfg.api_key) > 12 else "***"
        print(f"provider: {cfg.provider}")
        print(f"model:    {cfg.model}")
        print(f"api_key:  {masked}")
        print(f"base_url: {cfg.base_url}")
        return 0

    provider = args.provider or "nvidia"
    key = args.key
    if not key:
        key = getpass(f"Paste your {provider} API key (input hidden): ").strip()
    if not key:
        print("No key given — nothing saved.")
        return 1
    try:
        path = save_key(provider, key, model=args.model or "")
    except LLMError as exc:
        print(f"{exc}")
        return 1
    print(f"Key saved to {path}")
    print("Now run:  python -m cli status   — 'llm' line should show your provider.")
    return 0


def _cmd_voice_check(args: argparse.Namespace) -> int:
    # Diagnose the whole voice stack: deps, model, mic, voiceprint. Read-only.
    from system.voice import diagnostics

    report = diagnostics.voice_check()
    problems = []
    for section in report:
        status_mark = {"ok": "+", "warn": "!", "fail": "x"}.get(section["status"], "?")
        print(f"[{status_mark}] {section['name']}: {section['detail']}")
        for hint in section.get("hints", []):
            print(f"      {hint}")
        if section["status"] == "fail":
            problems.append(section["name"])
    print()
    if problems:
        print("Fix the failing items above, then re-run:  python -m cli voice-check")
        return 1
    print("All good. Now test end-to-end:  python -m cli listen  (say 'hey sura').")
    print("Watch the [stt] transcript lines — if your words are garbled, say so and "
          "we adjust the wake tokens or model.")
    return 0


def _cmd_autostart(args: argparse.Namespace) -> int:
    """Install/uninstall SURA's boot launcher in the Windows Startup folder."""
    from system.startup import StartupInstallError, startup_status, uninstall_startup, install_startup

    if args.status:
        info = startup_status()
        print("SURA auto-start:")
        for key in info:
            print(f"  {key}: {info[key]}")
        return 0
    if args.uninstall:
        if uninstall_startup():
            print("Auto-start removed.")
        else:
            print("No auto-start entry found (nothing to remove).")
        return 0
    try:
        tunnel = bool(getattr(args, "tunnel", False))
        pairing_key = _resolve_pairing_key(args.key or "") if (args.share or tunnel) else ""
        path = install_startup(with_voice=args.with_voice, share=args.share or tunnel,
                               pairing_key=pairing_key, tunnel=tunnel)
    except StartupInstallError as exc:
        print(f"Install failed: {exc}")
        return 1
    print(f"Auto-start installed: {path}")
    print("SURA's web UI now starts hidden every time you log in.")
    if args.with_voice:
        print("Voice mode: the wake-word listener starts with it and is kept "
              "alive (logs/voice-autostart.log).")
        print("The mic stays on while you are logged in — remove it with "
              "'python -m cli autostart --share' (without --with-voice).")
    if args.share:
        print("Shared mode: your phone can reach SURA after every reboot "
              "using the same pairing key.")
        print("Open it any time at http://127.0.0.1:8756 — the pairing key is "
              "stored in config/agent_config.yaml.")
    else:
        print("Open it any time at http://127.0.0.1:8756 (or pin it from Chrome: "
              "menu > Cast, save and share > Install page as app).")
    return 0


def _cmd_ping(args: argparse.Namespace) -> int:
    """Prove the LLM brain actually answers (one tiny real request)."""
    from system.llm.client import LLMError, LLMClient, load_config

    try:
        cfg = load_config()
    except LLMError as exc:
        print(f"Config error: {exc}")
        return 1
    if not cfg.enabled:
        print("No LLM configured (rule mode). Fix: python -m cli set-key nvidia <key>")
        return 1
    print(f"pinging {cfg.provider} / {cfg.model} …")
    try:
        client = LLMClient(cfg)
    except LLMError as exc:
        print(f"Client error: {exc}")
        return 1
    result = client.ping()
    if result["ok"]:
        print(f"BRAIN ONLINE — {cfg.provider}/{cfg.model} answered in "
              f"{result['latency_ms']} ms ({result['detail']})")
        return 0
    print(f"BRAIN DOWN — {result['detail']}")
    print("If this says 404/401/429, the key or endpoint is the problem, "
          "not the code: re-check the key on the provider's dashboard "
          "(credits, rate limits).")
    return 1


def _cmd_set_stt_model(args: argparse.Namespace) -> int:
    """Choose the Whisper model used for offline STT (persisted in config)."""
    from system.voice.stt_tts import _default_whisper_model, stt_engine_name

    if args.model:
        from system.llm.client import CONFIG_FILE, _parse_simple_yaml, _quote

        values = {}
        if CONFIG_FILE.exists():
            values = _parse_simple_yaml(CONFIG_FILE.read_text(encoding="utf-8"))
        values["stt_model"] = args.model
        CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_FILE.write_text(
            "\n".join(f"{key}: {_quote(str(val))}" for key, val in values.items()) + "\n",
            encoding="utf-8",
        )
        print(f"STT model set to '{args.model}' (saved in {CONFIG_FILE.name}).")
    else:
        print(f"STT engine: {stt_engine_name()}")
        print(f"whisper model: {_default_whisper_model()}")
        print("sizes: tiny.en (fastest) < base.en (default) < small.en "
              "(recommended for accented speech) < medium.en")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sura", description="SURA — Subhra's Unified Reasoning Assistant")
    sub = parser.add_subparsers(dest="command", required=True)

    thumb = sub.add_parser("thumbnail", help="Run the thumbnail workflow on a video topic (SURA's first specialty)")
    thumb.add_argument("objective", help="e.g. 'thumbnail for Valorant ranked highlights'")
    thumb.add_argument("topic", nargs="?", default="", help="Video topic (if not embedded in the objective)")
    thumb.add_argument("--channel", choices=["subhra-gaming"], default=None)
    thumb.add_argument("--tone", default=None, help="emotional tone: excited, shocked, happy, serious, ...")
    thumb.add_argument("--text", default=None, help="Short text overlay phrase")
    thumb.add_argument("--pattern", default=None, help="Preferred composition pattern")
    thumb.add_argument("--render", default=None, help="Path to a rendered image to run visual QA on")
    thumb.add_argument("--no-face", action="store_true", help="Creator will not appear in frame")
    thumb.set_defaults(func=_cmd_thumbnail)

    learn = sub.add_parser("learn", help="Record feedback using the learning progression")
    learn.add_argument("feedback")
    learn.add_argument("--scope", default="general")
    learn.set_defaults(func=_cmd_learn)

    chat = sub.add_parser("chat", help="Converse with the agent (LLM brain when configured)")
    chat.set_defaults(func=_cmd_chat)

    serve = sub.add_parser("serve", help="Start the local web UI (default http://127.0.0.1:8756)")
    serve.add_argument("--port", type=int, default=None)
    serve.add_argument("--share", action="store_true",
                       help="Also let devices on this Wi-Fi control SURA from a phone")
    serve.add_argument("--key", default=None,
                       help="Pairing key for --share (generated if omitted, saved to config)")
    serve.add_argument("--qr", action="store_true",
                       help="Print a scan-to-pair QR code (needs: pip install segno)")
    serve.add_argument("--tunnel", action="store_true",
                       help="Anywhere access via Tailscale (install Tailscale first); "
                            "keeps the pairing key enforced")
    serve.set_defaults(func=_cmd_serve)

    supervise = sub.add_parser(
        "supervise",
        help="Keep the web UI online: starts it and restarts it if it ever stops",
    )
    supervise.add_argument("--port", type=int, default=None)
    supervise.add_argument("--share", action="store_true",
                           help="Phone-accessible mode (keeps the pairing key enforced)")
    supervise.add_argument("--key", default=None,
                           help="Pairing key for --share (generated if omitted, saved to config)")
    supervise.add_argument("--tunnel", action="store_true",
                           help="Also advertise the Tailscale address (implies --share)")
    supervise.add_argument("--log", default=None,
                           help="Append the server's output to this file "
                                "(the login launcher uses logs/serve-autostart.log)")
    supervise.add_argument("--with-voice", action="store_true",
                           help="Also keep the wake-word listener alive (needs a mic)")
    supervise.add_argument("--voice-log", default=None,
                           help="Where the listener's output goes (defaults to --log)")
    supervise.set_defaults(func=_cmd_supervise)

    pushcmd = sub.add_parser("push", help="Send a note to every paired device (phone popup)")
    pushcmd.add_argument("text", help="Message to show on the phone, e.g. a link or reminder")
    pushcmd.set_defaults(func=_cmd_push)

    listen = sub.add_parser("listen", help="Wake-word voice mode: 'hey sura' + command")
    listen.add_argument("--download-model", action="store_true",
                        help="Download the offline STT model (~50 MB, one time)")
    listen.set_defaults(func=_cmd_listen)

    enroll = sub.add_parser("enroll-voice", help="Teach SURA your voice (speaker lock)")
    enroll.set_defaults(func=_cmd_enroll_voice)

    say = sub.add_parser("say", help="Speak text aloud")
    say.add_argument("text")
    say.set_defaults(func=_cmd_say)

    status = sub.add_parser("status", help="Show face library, brand, and memory status")
    status.set_defaults(func=_cmd_status)

    perms = sub.add_parser("permissions", help="Show the permission policy")
    perms.set_defaults(func=_cmd_permissions)

    setkey = sub.add_parser("set-key", help="Save an LLM API key permanently (survives reboots)")
    setkey.add_argument("provider", nargs="?", default=None, help="nvidia (default), groq, google, openai, anthropic, ollama")
    setkey.add_argument("key", nargs="?", default=None, help="The API key (omit to type it hidden)")
    setkey.add_argument("--model", default=None, help="Override the provider's default model")
    setkey.add_argument("--list", action="store_true", help="Show the current configuration (key masked)")
    setkey.set_defaults(func=_cmd_set_key)

    voicecheck = sub.add_parser("voice-check", help="Diagnose the voice stack (deps, model, mic, voiceprint)")
    voicecheck.set_defaults(func=_cmd_voice_check)

    ping = sub.add_parser("ping", help="Test that the LLM brain actually answers (one tiny request)")
    ping.set_defaults(func=_cmd_ping)

    sttmodel = sub.add_parser("set-stt-model", help="Show or set the Whisper STT model (e.g. small.en)")
    sttmodel.add_argument("model", nargs="?", default=None,
                          help="tiny.en, base.en, small.en, medium.en …")
    sttmodel.set_defaults(func=_cmd_set_stt_model)

    autostart = sub.add_parser("autostart", help="Start SURA's web UI automatically at login (Windows)")
    autostart.add_argument("--uninstall", action="store_true", help="Remove the auto-start entry")
    autostart.add_argument("--status", action="store_true", help="Show auto-start status without changing it")
    autostart.add_argument("--with-voice", action="store_true",
                           help="Also start wake-word listening at login (experimental)")
    autostart.add_argument("--share", action="store_true",
                           help="Start in shared mode so your phone keeps access after reboots")
    autostart.add_argument("--key", default=None,
                           help="Pairing key for --share (generated if omitted, saved to config)")
    autostart.add_argument("--tunnel", action="store_true",
                           help="Anywhere access via Tailscale at every login")
    autostart.set_defaults(func=_cmd_autostart)

    return parser


def _make_console_safe() -> None:
    """Never let a console encoding kill SURA.

    LLM replies contain characters outside the console's codepage (a
    non-breaking hyphen U+2011 ended a live voice session mid-sentence:
    UnicodeEncodeError from a plain print()). Replace what cannot be shown
    instead of raising.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):  # not a reconfigurable stream
            continue


def main(argv: list[str] | None = None) -> int:
    _make_console_safe()
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
