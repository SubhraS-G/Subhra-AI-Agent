# Projects — SURA: Subhra's Unified Reasoning Assistant

**Personal AI agent — local-first, permission-gated, voice-aware**
A J.A.R.V.I.S.-class assistant built around a creator's workflows, brands, and technical work — text, CLI, voice ("hey sura"), and a web command center. Free-first by design: the LLM brain runs on free API tiers (NVIDIA NIM, Groq, Gemini) or a local Ollama model, the voice layer is fully offline, and the core system is pure Python standard library. No credit card anywhere.

---

## What it is

SURA is a personal AI system that understands how Subhra works, remembers preferences, executes repetitive tasks, assists with gaming-content and game-dev work, and grows from an assistant into a semi-autonomous operator. It is built around five.MD constitution files (AGENT_SPEC, BEHAVIOR_RULES, PERMISSIONS, USER_PROFILE, MEMORY) that are loaded at runtime and also fed to the LLM brain as its system prompt — the code and the model reason from the same source of truth.

The core loop is `UNDERSTAND → PLAN → EXECUTE → VERIFY → LEARN`, with a permission gate that the LLM cannot bypass because it lives inside the tools, not in a prompt. Brand separation is enforced as a hard failure in code. Currently only
Subhra Gaming is an active channel; no second brand is in the system. The learning system treats one correction as a temporary preference, not a permanent rule — confidence rises with repeated evidence.

**Where it stands:** core + LLM brain + SQLite memory + web UI + voice mode built and tested. On the roadmap: image rendering for thumbnails, YouTube/GitHub connectors, proactive briefings.

---

## Architecture

```
USER → ORCHESTRATOR → MEMORY + PLANNER + RULES → SPECIALIST AGENTS → TOOLS → APPROVAL GATE → ACTION → VERIFIED RESULT
```

| Layer | Where it lives | What it does |
|---|---|---|
| Core loop + routing | `system/orchestrator.py` | UNDERSTAND/PLAN/EXECUTE/VERIFY/LEARN, routes to specialists, enforces the permission gate before any action |
| LLM brain (Tier 2a) | `system/llm/` | provider-agnostic chat client, agent loop with tool calling, system prompt built from the core MD files |
| Memory | `system/sqlite_memory.py` (default) + `system/memory.py` (file fallback) | SQLite with FTS5 full-text search, note cross-links, correction log, confidence-scored preferences; migrates existing notes automatically |
| Permission gate | `system/permissions.py` | automatic / approval / explicit tiers, derived from PERMISSIONS.md, unknown actions fail closed |
| Brand registry | `system/brands.py` | Subhra Gaming resolution + separation assertion; hard-fail on mixing; only one active channel right now |
| Thumbnail Agent | `system/agents/creative/` | brief → face selection → ranked concepts → composition plan → brand sheet → QA → review package |
| Editing Agent (Tier 4 v1) | `system/agents/editing/` | learn-editing-style + build-edit-plan — style profiles distilled from pasted notes/links, per-video edit plans written to disk |
| Voice layer (Tier 3) | `system/voice/` | wake word, offline STT (faster-whisper, Vosk fallback), offline TTS (pyttsx3/SAPI5), speaker verification so it answers only to Subhra |
| Web UI | `ui/` + `system/server.py` | three-panel HUD: chat with live token streaming, status, memory search, packages, preferences, editing plans; installable PWA; phone-accessible with pairing-key gate |
| CLI | `cli/` | chat, serve, thumbnail, learn, status, permissions, listen, enroll-voice, say, push, set-key, voice-check, autostart, supervise, ping, set-stt-model |
| Auto-start | `system/startup.py` + `system/supervisor.py` | hidden boot launcher (pythonw + VBS into the Startup folder) + watchdog that keeps the server (and optionally the wake-word listener) alive across crashes/reboots |

---

## What was built

### Core agent system
- **Orchestrator** with the full five-step loop, session tracking, permission enforcement before execution, and verification-gated reporting (never claims a result was completed unless verified). Fallback from SQLite memory to file memory if the DB is unavailable.
- **Permission gate** with three tiers (automatic / approval / explicit), an action registry derived from PERMISSIONS.md, and fail-closed handling for unknown actions. Publishing, sending, spending, deleting, and pushing all return "needs approval" — whether the LLM asks or not.
- **Brand registry** that resolves a brand from free text (objective, channel name), asserts separation on every request, and raises a hard `BrandError` when both brands appear in one prompt. Identity status (filled vs. unfilled VISUAL_IDENTITY.md) is surfaced everywhere.
- **Verification helpers** used by the orchestrator and the QA agent — a `Verification` object carries named checks with PASS/FAIL and an overall verdict; nothing is reported as done unless the checks pass.

### LLM brain (free-first, provider-agnostic)
- **Client** (`system/llm/client.py`) speaks plain HTTPS — no SDK install required — and supports NVIDIA NIM, Groq, Google Gemini, OpenAI, Anthropic, and local Ollama through one interface. A tiny custom YAML parser reads `config/agent_config.yaml`; a dotenv auto-loader reads `config/.env`. Config resolution order: explicit provider env → auto-detected first key (NVIDIA prioritized) → file → no provider = rule mode. Multiple keys build an automatic **fallback chain** (NVIDIA → Groq → Gemini) for rate limits and exhausted credits.
- **Agent loop** (`system/llm/agent_loop.py`) runs UNDERSTAND/PLAN/EXECUTE/VERIFY/LEARN with the LLM doing understanding and planning and the tools doing verified execution. Bounded to `max_turns` so a confused model can never spin forever. Tries each configured provider in order on failure; streams token-by-token with provider-aware SSE parsing and tool-call fragment assembly; carries partial text through `StreamInterrupted` so a mid-stream dropout still yields what arrived.
- **Tool layer** (`system/llm/tools.py`) exposes every existing capability as provider-neutral `ToolSpec`s with JSON-schema parameters and handlers that run the permission gate internally — the LLM never sees the raw capability, so it cannot bypass approval. Tools: `run_thumbnail_workflow`, `record_feedback`, `search_memory`, `read_memory`, `write_memory`, `check_approval`, `agent_status`, `learn_editing_style`, `build_edit_plan`, `run_pc_command` (with destructive-pattern blocking), `open_app`, `push_to_phone`. Anthropic and OpenAI message shapes are translated per provider.
- **System prompt builder** renders the core MD files (clipped defensively) plus live memory and capability docs into the prompt, so the brain reasons from the same constitution the code enforces.
- **Key persistence**: `python -m cli set-key nvidia <key>` stores to `config/agent_config.yaml` (gitignored) so every new terminal, the web UI, and voice mode all see it. `cli ping` proves the brain actually answers — a saved key is not the same as a working brain.

### Memory (Tier-1 finisher)
- **SQLite store** (`system/sqlite_memory.py`) as a drop-in replacement for the file store, with the same public interface plus FTS5 full-text search across notes and preferences, note cross-links, a full correction log, and automatic import of existing `memory/*.md` notes. Falls back to LIKE search on older SQLite without FTS5. Thread-safe for the shared server (`check_same_thread=False` + a lock serializing mutations). FTS5 ranking + BM25 when available.
- **File store** (`system/memory.py`) keeps the four documented memory classes (permanent / projects / workflows / temporary) as Markdown, plus confidence-scored preferences with a documented promotion ladder (one correction = low; repeated evidence promotes toward established). One correction never becomes a permanent rule.
- Both stores implement the same interface, so the rest of the system is storage-agnostic.

### Thumbnail workflow (first specialty)
The agent executes the 13-step workflow end to end and writes a review package:

1. Identify channel (brand resolution + separation assertion)
2. Understand video topic
3. Determine emotional tone (keyword inference + explicit override)
4. Create a structured, validated thumbnail brief (`ThumbnailBrief` — required fields, short-overlay enforcement, brand slug required)
5. Select face references by tone (when the creator is in frame; empty library blocks creator-in-frame tasks with a clear message)
6. Generate 3–5 concepts from `COMPOSITION.md` patterns
7. Rank concepts (pattern match, content type, requested overlay)
8. Generate/edit composition (render plan, renders via an image tool when connected)
9. Integrate branding (brand sheet with filled/unfilled status; separation enforced)
10. Run visual QA — technical checks on real files (format, aspect ratio, resolution); perceptual checks emitted as an explicit review checklist, never claimed as passes the agent can't observe
11. Produce variants (A/B/C/D plan)
12. Present for user review (package written to `thumbnail-system/output/<date>_<topic>/package.md`)
13. Record useful feedback through the learning progression

The package is a complete pre-render brief: brief, face picks, ranked concepts with rationale, layer-by-layer composition plan, brand integration sheet, variant plan, QA status, and small-scale readability note.

### Video-editing assist (Tier 4, v1)
- **StyleBook** distills pasted study notes / YouTube links into style profiles (pacing, cuts, zooms, effects, sound design) — parses numbers, links, and descriptive phrases; merges multiple pastes into the same creator profile; recognizes known creators and falls back to a derived name. Persisted to `editing/styles.json`.
- **Edit plans** are generated per video topic from the merged style profile + gear constraints, written to `editing/plans/<date>_<topic>.md` with hook, pacing targets, zoom recipes, effects, sound design, text callouts, and an export checklist. Each number is labeled as coming from the style profile or a default. Pure string work — no LLM required, so it works in rule mode and is fully testable.
- Exposed to the LLM as `learn_editing_style` and `build_edit_plan` tools, and to the web UI as an editing panel that appears once styles exist.

### Web UI — "Stark-HUD" command center
- Vanilla HTML/CSS/JS, no dependencies. Three panels (chat / packages / status) that collapse to bottom tabs on phones.
- **Streaming chat**: NDJSON over a POST stream — the phone sees the first words in roughly one round trip instead of after the whole generation. Falls back to the classic whole-reply endpoint when streaming isn't available. Token stream fills a live bubble as words arrive, then swaps in the full markdown-rendered version.
- **Pairing-key gate** for LAN/share mode: short, human-typeable 8-character key (no ambiguous chars), `X-SURA-Key` header, `secrets.compare_digest`, 10-attempt / 5-minute lockout, loopback always trusted, `/api/set-key` refused from remote devices. Persisted to config so a paired phone survives reboots.
- **Status panel** live-reports LLM provider + real health check (`cli ping` under the hood, 60s cache), voice enrollment + listening state, face library totals and missing expressions, brand identity filled/unfilled, memory preferences + stats, Tailscale anywhere-access state, editing styles + plans.
- **Push inbox** (laptop ↔ phone): `python -m cli push "..."` drops a note that paired devices poll and show as a toast + optional system notification. Phone side polls every 5s with silent raw fetch so a missing key doesn't pop the pairing prompt.
- **Voice mirror**: every spoken turn (yours and SURA's) is recorded and mirrored onto the chat screen, so a voice exchange can be read later and continued by typing.
- **PWA**: installable taskbar app via `beforeinstallprompt` / `appinstalled`, with a stdlib-generated service worker (app-shell cache only; `/api/*` always hits the live server) and pure-stdlib PNG icons (zlib + struct) of the orb.
- **Brain pill + key save**: paste an NVIDIA/Groq/Gemini key into the UI and it saves to config, verifies the brain answers, and reports latency. PC-only while shared.

### Voice mode (Tier 3, built)
- **Wake word** "hey sura" with fuzzy matching: measured real-world Whisper mishearings ("hey sir", "hey so", "hey sara", "soora") all wake SURA; a bare "sir" mid-sentence does not.
- **Speaker verification**: enroll 3 samples → voiceprint (`voice/voiceprint.json`, gitignored); every activation is verified against it before any command runs; a different voice that says the wake word gets a spoken refusal and is logged. Default threshold 0.90 cosine similarity, configurable in `agent_config.yaml` (`voice_threshold`), clamped 0.50–0.99. Without enrollment, SURA accepts any voice (explicit open mode, printed as a warning).
- **Offline STT**: faster-whisper (recommended, far better with accents than Vosk), model downloads itself on first use and caches in `voice/models/whisper/`. Vosk as automatic fallback. Configurable model size (`cli set-stt-model small.en`).
- **Offline TTS**: pyttsx3 / SAPI5 on Windows; each utterance speaks through a fresh helper process (platform speech only works once per process) with an inline fast path when already in the right process.
- **Voice loop** routes commands through the same orchestrator as chat/CLI — voice gets the LLM brain, tools, permission gate, and verification. A failed command does not kill the listener; the loop recovers. The lazy orchestrator import is reachable (regression: it used to live inside `__init__` and raise `NameError` on the first real command).
- **Transcriber resilience**: decode failures fall back to Vosk instead of dying; Whisper receives a float32 numpy array, never raw bytes handed to PyAV (which blew up on some builds). Overlap windows are deduplicated so the same prefix is never emitted twice; silent feeds never decode (VAD).
- **Supervised voice**: `autostart --with-voice` keeps the wake-word listener alive across crashes and reboots the same way the server is supervised, with its own log (`logs/voice-autostart.log`) and backoff.

### LAN / anywhere access
- `python -m cli serve --share` binds to the LAN, generates a pairing key, and requires it from every non-loopback request. The PC itself is always trusted.
- `serve --share --tunnel --qr` adds Tailscale anywhere access: a stable tailnet address reachable from any network (mobile data included), with the pairing key still enforced and `X-Forwarded-For` honored behind the local HTTPS proxy. Chrome refuses to install PWAs from plain HTTP, so the README documents Tailscale Serve for a real certificate and a true installable app.
- `python -m cli autostart --share --qr` (and `--with-voice`, `--tunnel`) starts SURA hidden at every login and keeps it online; `cli supervise` restarts the server if it ever stops, and only one supervisor ever runs (lock file, released on crash). Two logs on purpose: `serve-autostart.log` (SURA's own output) and `serve-launcher.log` (anything that fails before SURA starts) — the launcher redirect is explicit because a windowless child does not inherit a redirected stdout.

### CLI
Full command set: `chat`, `serve`, `supervise`, `thumbnail`, `learn`, `status`, `permissions`, `push`, `listen`, `enroll-voice`, `say`, `set-key` (with `--list` and masked interactive input), `voice-check`, `ping`, `set-stt-model`, `autostart` (with `--status`, `--uninstall`, `--share`, `--with-voice`, `--tunnel`, `--key`). Console encoding is hardened (`errors="replace"`) so a non-printable character in a reply (a non-breaking hyphen once ended a live voice session) does not crash the CLI.

### Configuration & platform
- Keys persist to `config/agent_config.yaml` (gitignored); dotenv in `config/.env` (gitignored, auto-loaded). Voiceprint, memory DB, config keys, and logs are gitignored.
- Logs are structured JSON-lines in `logs/<date>.log` with no secrets.
- Server is pure stdlib (`http.server` + threading), quiet by default, with a `QuietHTTPServer` that logs disconnected clients in one line instead of a full traceback.
- PWA assets (manifest, service worker, icons) are generated on first serve so a fresh checkout installs cleanly.

---

## What makes the system "honest" by design

- **Verification before claims** — the orchestrator sets result status from actual verification, not from the agent's say-so; QA perceptual checks are emitted as a review checklist, never claimed as passes the agent can't observe.
- **Permission gate in code, not in a prompt** — the LLM never sees the raw capability, so it cannot bypass approval for publish/send/spend/delete/push.
- **Brand separation as a hard failure** — mixing Subhra Gaming and GTA V Labs in one request raises `BrandError` before any work starts.
- **Learning discipline** — one correction is a temporary preference (low confidence); confidence rises with repeated evidence toward established.
- **Local-first privacy** — face library, personal memory, voiceprint, and screen data stay on the machine; the only external calls are to the LLM provider you configure.

---

## Test suite

Hermetic — no network, no mic, no API keys needed.

- **Core system tests**: memory learning progression (single correction = low; repeated corrections promote), permissions (automatic allowed, publish blocked at automatic, unknown actions fail closed), brand resolution + separation + unknown-brand rejection, brief validation (long overlay flagged), face library empty-library guard, orchestrator end-to-end thumbnail request producing a package, missing-channel request returning `needs_input`.
- **Key/config tests**: save-key writes YAML and round-trips, preserves other settings, rejects unknown providers, special characters round-trip, env wins over file, dotenv autoloaded, existing env not overridden.
- **Autostart/supervisor tests**: install/uninstall, status reflects install, voice mode flag, share key + tunnel flags, port-open reflects listener state, serve command flags, single-supervisor lock, exits when another supervisor runs, relaunches a dead server, backs off a child that keeps dying, sends child output to the log, never starts a second server.
- **Voice loop tests**: wake-then-command executes, stranger rejected before execution, wake-only gets a prompt, non-wake ignored, unenrolled system still executes text commands, listen creates a speaker so replies are audible, lazy orchestrator reachable, a failed command does not end voice mode.
- **Transcriber resilience tests**: decode failure falls back to Vosk instead of dying, Whisper receives an array not bytes, dedup of overlap, silent feed never decodes.
- **Speaker verification tests**: enroll and accept owner, reject different voice, reject noise, fail closed without enrollment, persists to disk.
- **LLM tests**: config load, save-key, URL join (regression: `/v1` base must not produce `/v1/v1/...`), streaming SSE chunks → text events, SSE tool-fragment accumulation, stream interruption carries partial text, non-streaming provider falls back to one event, agent-loop stream falls back between providers, `_normalize_anthropic` / `_normalize_openai`.
- **PC-tool tests**: run command captures output, blocks destructive patterns, empty command errors, open-app requires name, push-to-phone roundtrip writes to inbox.
- **Editing tests**: learn parses numbers and links, merges into same profile, merged overrides baseline, plan written to disk, agent handle plan mode, plan read name validation.
- **App tests**: app mirrors spoken turns into chat, app boot waits for the server.
- Voice diagnostics tests, voice threshold config tests (config knob changes threshold, bad value falls back, explicit arg wins).

---

## Project structure

```
AGENT_SPEC.md            master specification (the constitution)
BEHAVIOR_RULES.md        how SURA must behave
PERMISSIONS.md           what requires approval — enforced by code
USER_PROFILE.md          Subhra's profile (SURA reads this at startup)
MEMORY.md                memory design
ROADMAP.md               the five-tier growth plan (tool user → operator)
SYSTEM.md                implementation guide (architecture ↔ code map)

system/                  the agent core
  orchestrator.py        core loop + routing + chat
  sqlite_memory.py       default memory store (SQLite, FTS5, links, corrections)
  memory.py              file-based memory store (fallback / compatible interface)
  permissions.py         permission gate
  brands.py              brand registry + separation enforcement
  verification.py        verification helpers (PASS/FAIL checks → verdict)
  core_docs.py           loads the five root MD files (cached)
  paths.py               all runtime paths, resolved from the system root
  logging_utils.py       structured JSON-lines logging
  startup.py             Windows auto-start launcher (pythonw + VBS)
  supervisor.py          watchdog that keeps server (+ optional voice) alive
  llm/
    client.py            provider-agnostic chat client (HTTPS, no SDK)
    agent_loop.py        tool-using loop with fallback chain + streaming
    tools.py             provider-neutral ToolSpecs + dispatch + permission gate
    system_prompt.py     builds the system prompt from core MD files + memory
  voice/
    assistant.py         wake-word loop: verify → command → spoken reply
    wake.py              fuzzy wake matching + speaker verifier (cosine similarity)
    stt_tts.py           offline STT (faster-whisper/Vosk) + TTS (pyttsx3) + speaker
    diagnostics.py       read-only voice stack check (deps, model, mic, voiceprint)
  agents/
    base.py              SpecialistAgent base + TaskResult
    registry.py          routes objectives to specialists
    creative/            Thumbnail Agent (brief, concepts, face library, QA)
    editing/             Editing Agent (style profiles + per-video edit plans)

cli/                     command-line interface
  __main__.py            all subcommands

ui/                      web UI (vanilla HTML/CSS/JS, PWA)
  index.html
  style.css
  app.js                 chat, status, packages, preferences, search, editing, push, voice mirror
  manifest.webmanifest   (generated)
  sw.js                  (generated)
  icon-192.png / icon-512.png   (generated, pure-stdlib PNG)

brands/
  subhra-gaming/         VISUAL_IDENTITY.md (template)
  (gta-v-labs/ removed — channel paused; only Subhra Gaming remains active)

face-library/            real face photos by expression (input needed)
thumbnail-system/        rules, references, generated packages
editing/                 style profiles (styles.json) + per-video edit plans
memory/                  SQLite store (gitignored) + push inbox + voice transcript
logs/                    structured JSON-lines logs (gitignored)
config/                  agent_config.yaml + .env (gitignored)
tests/                   105+ hermetic tests
```

---

## The roadmap context (where this fits)

The project is organized around five tiers: **T1** tool user, **T2** connected, **T3** aware, **T4** proactive, **T5** operator.

**Built now:** T1 foundation (permission gate, brand separation, thumbnail agent, CLI, tests), T2a LLM brain (provider-agnostic client, agent loop, tool calling, system prompt from the core docs), T3 voice (wake word + offline STT/TTS + speaker verification, fully built and tested), T4 v1 editing assist (learn-editing-style + build-edit-plan), and the taskbar app + auto-start + supervise watchdog.

**On the roadmap (not yet built):** T1 finisher items that need Subhra's input (USER_PROFILE filled, face photos in all 8 expression folders, both VISUAL_IDENTITY.md files filled, 10–20 liked/disliked references), T2b YouTube + GitHub integration, T2c image rendering (ComfyUI/SDXL+ControlNet or an API, with facial-identity preservation as a hard rule and automated visual QA), T3 always-on memory + screen/computer use + notifications in + context engine, T4 morning briefing + trend watch + autonomous pre-work + initiative budget + multi-machine reach, T5 end-to-end video pipeline + game-dev copilot + self-improvement loop + long-horizon planning.

The honest constraint documented in the roadmap: tiers 1–2 are engineering; tiers 3–5 are gated by three real inputs only Subhra can provide — API keys/budget (or a GPU for local models), his data (profile, faces, references, analytics access), and his trust (the initiative budget).

---

## Non-user inputs still needed to complete the picture

These are the things only Subhra can provide; the code is ready for them:

1. `USER_PROFILE.md` — fully filled (identity, goals, gear, schedule, preferences). It already has a solid base from a 2026-09-30 Q&A session.
2. Face photos in all 8 expression folders under `face-library/` (neutral, happy, excited, shocked, angry, confused, scared, serious) + optional `metadata.yaml`.
3. Both `brands/*/VISUAL_IDENTITY.md` files filled in (colors, typography, thumbnail style, logo rules, effects).
4. 10–20 liked/disliked thumbnails in `thumbnail-system/references/`.
5. An LLM API key of choice (NVIDIA NIM, Groq, Gemini, or local Ollama) — free to start, no card.

---

## A note for the CV

If you want a shorter, resume-paragraph version of this, here's the shape I'd give it:

> **SURA — personal AI agent (Python, standard library)** — built a local-first, permission-gated assistant with a provider-agnostic LLM brain (NVIDIA/Groq/Gemini/Ollama via plain HTTPS, no SDK), SQLite memory with FTS5 search and confidence-scored learning, a voice layer that is fully offline (wake word + faster-whisper STT + pyttsx3 TTS + speaker verification so it answers only to one voice), and a web command center that streams replies live and is an installable PWA with phone access gated by a pairing key. Implemented the core UNDERSTAND→PLAN→EXECUTE→VERIFY→LEARN loop, a three-tier permission gate enforced in code (not in a prompt), hard-fail brand separation between two creator channels, a 13-step thumbnail workflow that produces a full pre-render review package, and a video-editing assist that distills style profiles from pasted notes and writes per-video edit plans. Wrote a hermetic test suite (105+ tests, no network/mic/keys) covering the core system, LLM client and streaming, voice loop and speaker verification, supervisor/auto-start, and PC-control tools. Free-first: the brain runs on free API tiers or a local model, the voice layer is fully offline, and the core system is pure Python standard library.

---

*Generated from the SURA project as it stands. Update this file when a milestone lands — especially after an LLM key is wired in, after image rendering lands, and after any YouTube/GitHub connector is built.*
