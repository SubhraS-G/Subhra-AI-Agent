# SURA — SUBHRA'S UNIFIED REASONING ASSISTANT

The working implementation of the specification in `AGENT_SPEC.md`.
The agent's name is **SURA** (see `AGENT_NAME` in `system/paths.py`).

## Architecture (AGENT_SPEC §13)

```
USER → ORCHESTRATOR → MEMORY + PLANNER + RULES → SPECIALIST AGENTS → TOOLS → APPROVAL GATE → ACTION → VERIFIED RESULT
```

| Layer | Code | Spec source |
|---|---|---|
| Core loop (UNDERSTAND → PLAN → EXECUTE → VERIFY → LEARN) | `system/orchestrator.py` | AGENT_SPEC §2 |
| **LLM brain (Tier 2a)** | `system/llm/` — client, agent loop, tools, system prompt | ROADMAP.md Tier 2a |
| Memory (permanent / project / workflow / temporary) | `system/sqlite_memory.py` (default) or `system/memory.py` files | MEMORY.md |
| Learning progression (correction → … → established) | `system/memory.py`, `system/sqlite_memory.py` | MEMORY.md, AGENT_SPEC §11 |
| Permission gate (automatic / approval / explicit) | `system/permissions.py` | PERMISSIONS.md |
| Brand separation (only Subhra Gaming active) | `system/brands.py` | AGENT_SPEC §9 |
| Verification reporting | `system/verification.py` | AGENT_SPEC §2, BEHAVIOR_RULES |
| Specialist agents + routing | `system/agents/` | AGENT_SPEC §5 |
| Thumbnail Agent (first specialist) | `system/agents/creative/` | AGENT_SPEC §6–8, `thumbnail-system/` |

The five root MD files (`AGENT_SPEC`, `BEHAVIOR_RULES`, `PERMISSIONS`,
`USER_PROFILE`, `MEMORY`) remain the single source of truth and are loaded
at runtime by `system/core_docs.py`.

## Quick start

```bash
# Talk to the agent (LLM brain when configured, guidance otherwise)
python -m cli chat

# Web UI (three-panel HUD: chat, status, packages, memory, preferences)
python -m cli serve        # -> http://127.0.0.1:8756 (private to this PC)

# Phone access on the same Wi-Fi (pairing-key protected LAN share)
python -m cli serve --share --qr   # prints phone URL(s) + scan-to-pair QR

# Keep the web UI online (this is what the login launcher runs: it starts the
# server and restarts it if it ever exits, with exactly one instance)
python -m cli supervise

# Start SURA hidden at every login — the app window then always finds it live
python -m cli autostart --share

# Run the thumbnail workflow (13 steps from workflows/thumbnails/THUMBNAIL_WORKFLOW.md)
python -m cli thumbnail "Valorant ranked highlights" --channel subhra-gaming

# Options: --tone, --text, --pattern, --render <path>, --no-face
python -m cli thumbnail "Valorant ranked highlights" --channel subhra-gaming --tone excited

# Record feedback (learning progression: 1 correction = temporary preference)
python -m cli learn "avoid red overlays on GTA V Labs thumbnails" --scope thumbnails

# Inspect system state and permission policy
python -m cli status
python -m cli permissions

# Run tests
python -m unittest discover -s tests
```

## LLM brain setup — free-first

The agent runs fine without an LLM (rule-based mode). All recommended
providers are **free to start, no credit card**.

**Easiest way (Windows-friendly, permanent):**

```bash
python -m cli set-key nvidia nvapi-...   # from build.nvidia.com
python -m cli set-key --list             # verify (key shown masked)
```

The key is saved to `config/agent_config.yaml` (gitignored) and picked up
by every new terminal, the web UI, and voice mode — no `export`/`set`
needed. You can also paste the key straight into the web UI's "paste LLM
API key" box. Or put `NVIDIA_API_KEY=...` in `config/.env` (gitignored,
auto-loaded). A real environment variable still wins over the file.

Prefer env vars? These work too (bash syntax; on CMD use `set`):

Set several keys and the agent builds a **fallback chain**: NVIDIA ->
Groq -> Gemini. When one hits a rate limit or runs out of credits, the
next takes over automatically.

Or copy `config/agent_config.example.yaml` → `config/agent_config.yaml`
and fill it in (gitignored). Then run `python -m cli chat`.

When enabled, the LLM gets your core MD files as its constitution and can
call tools: `run_thumbnail_workflow`, `record_feedback`, `search_memory`,
`read_memory`, `write_memory`, `check_approval`, `agent_status`. The
permission gate runs inside the tools — the LLM cannot bypass it.

## Memory

Default store is SQLite (`memory/memory.db`, gitignored) with full-text
search, note cross-links, and a full correction history. Existing markdown
memory is imported automatically on first migration; the original file-based
store remains available (`system/memory.py`).

## Voice — wake word + speaker lock (Tier 3)

SURA can run always-listening: dormant until it hears **"hey sura"**, then
it takes one spoken command and replies out loud. All offline and free.

```bash
# One-time setup (free):
pip install -r requirements-voice.txt
python -m cli enroll-voice                   # teach SURA YOUR voice (3 samples)

# Every day:
python -m cli listen                         # SURA waits for "hey sura …"
python -m cli voice-check                    # diagnose deps/model/mic/voiceprint
python -m cli say "Systems online"           # speak text directly
```

STT is **faster-whisper** (offline Whisper on CPU — far better with accents
than the old Vosk default). The model downloads itself on first use and is
cached in `voice/models/whisper/`. Upgrade accuracy with
`python -m cli set-stt-model small.en`; Vosk remains as an automatic fallback.

Is the brain really answering? `python -m cli ping` sends one tiny request
and prints latency — a saved key is not the same as a working brain.

## Phone access (LAN share mode)

`--share` binds the server to `0.0.0.0` (the LAN) and generates a short,
unambiguous **pairing key** (`generate_pairing_key()` in `system/server.py`).
Every non-loopback request must present it in the `X-SURA-Key` header; the
PC itself is always trusted. Keys are sanitized (`sanitize_key`) and compared
with `secrets.compare_digest`; repeated failures (10 within 5 minutes) get a
lockout. The pairing key is persisted to `config/agent_config.yaml`
(`pairing_key:`) so `serve` and `autostart --share` reuse the same value and
a paired phone never re-pairs after a reboot. While shared:

- `/api/set-key` refuses non-loopback requests — the phone cannot replace
  the LLM API key.
- `/api/pair?key=…` validates a candidate key without exposing anything.
- The phone UI (ui/app.js) stores the key in `localStorage`, attaches it to
  every call, accepts `?key=…` URLs (the QR code's payload), and prompts on
  401. On narrow/touch screens the UI switches to a single-column chat with
  a bottom tab bar (chat / packages / status).

**Only-your-voice lock:** `enroll-voice` builds a voiceprint
(`voice/voiceprint.json`, gitignored) from spectral voice features. Every
activation is verified against it before any command runs; a different
voice that says the wake word gets a spoken refusal and is logged. Without
enrollment SURA accepts any voice (your explicit choice, printed as a
warning at startup). The verification threshold lives in
`SpeakerVerifier` (default 0.90 cosine similarity).

The loop (`system/voice/assistant.py`) routes commands through the same
orchestrator as chat/CLI — voice gets the LLM brain, tools, permission
gate, and verification as every other input path.

## Thumbnail pipeline

`Real photograph → subject extraction → editing → compositing → environment integration → QA`

- **Brief** (`brief.py`): structured contract with validation (short text, required fields, channel).
- **Face library** (`face_library.py`): indexes `face-library/<expression>/` and `metadata.yaml`;
  selects references by tone. Empty library blocks creator-in-frame tasks with a clear message.
- **Concepts** (`concepts.py`): generates and ranks 3–5 concepts from the COMPOSITION.md patterns.
- **QA** (`qa.py`): executes technical checks (format, aspect ratio, resolution) on real files;
  perceptual checks (identity, artifacts, clutter) are emitted as an explicit review
  checklist — the agent does not claim perceptual passes it cannot observe.
- **Output**: `thumbnail-system/output/<date>_<topic>/package.md` — brief, face picks,
  ranked concepts, layer-by-layer render plan, brand sheet, variant plan, QA status.

Rendering plugs in at step 8 when an image-generation/editing tool is connected;
until then the agent produces the complete pre-render package and runs technical
QA on any supplied render (`--render path/to/image.png`).

## What needs input from Subhra

1. `USER_PROFILE.md` — fill in identity, goals, preferences.
2. `face-library/<expression>/` — add real photographs (8 expressions recommended).
3. `face-library/metadata.yaml` — optional per-photo metadata (angle, lighting, suitability).
4. `brands/*/VISUAL_IDENTITY.md` — colors, typography, logo rules per channel.
5. `thumbnail-system/references/liked|disliked/` — style references.

`python -m cli status` reports which of these are still empty.

## Logs

Structured JSON-lines in `logs/<date>.log` (sessions, plans, permission
blocks, task results). No secrets are ever logged.
