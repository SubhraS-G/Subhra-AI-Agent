# SURA — Subhra's Unified Reasoning Assistant

A personal AI agent built around Subhra's workflows, preferences, creator brands, and technical work — designed to grow from an assistant into a semi-autonomous operator, in the spirit of J.A.R.V.I.S. / F.R.I.D.A.Y. / E.D.I.T.H.

**Free-first by design:** the LLM brain runs on free API tiers (NVIDIA NIM, Groq, Google Gemini) or a local Ollama model, the voice layer is fully offline, and the core system is pure Python standard library. No credit card anywhere.

```
Text · CLI · Voice ("hey sura") · Web UI
        │
        ▼
ORCHESTRATOR ── UNDERSTAND → PLAN → EXECUTE → VERIFY → LEARN
        │
MEMORY + RULES + PERMISSION GATE
        │
        ▼
SPECIALIST AGENTS  →  TOOLS  →  VERIFIED RESULTS
```

## Features

| | |
|---|---|
| 🧠 **LLM brain** | Provider-agnostic chat with tool calling; your MD files are its constitution |
| 🗂 **Compounding memory** | SQLite with full-text search, cross-links, and confidence-scored learning from your corrections |
| 🔐 **Permission gate** | Automatic / approval / explicit tiers enforced in code — the LLM cannot bypass them |
| 🖼 **Thumbnail Agent** | The first specialty: brief → face selection → ranked concepts → render plan → QA → review package |
| 🏷 **Brand separation** | Only Subhra Gaming is active right now; no other channel is mixed in — hard-fail check |
| 🎨 **Web UI** | Stark-HUD command center: chat with live tool traces, status, memory search, preferences |
| 🎙 **Voice mode** | Always-listening wake word ("hey sura"), offline STT/TTS, and a **speaker lock** so it answers *only* to your voice |
| 🧪 **114 tests** | Fully hermetic suite — no network, no mic, no API keys needed |

## Quick start

Requires Python 3.11+ (developed on 3.14). No dependencies needed for core + UI.

```bash
# Talk to SURA (rule-based mode until an LLM key is set)
python -m cli chat

# Web command center
python -m cli serve            # → http://127.0.0.1:8756

# The first specialty: the thumbnail workflow
python -m cli thumbnail "Valorant ranked highlights" --channel subhra-gaming --tone excited

# Teach it a standing preference
python -m cli learn "minimal text on thumbnails" --scope thumbnails

# Inspect state
python -m cli status
python -m cli permissions
```

### Enable the LLM brain (free)

```bash
export NVIDIA_API_KEY=nvapi-...   # build.nvidia.com — free credits, no card (auto-detected)
# or GROQ_API_KEY / GEMINI_API_KEY, or SUBHRA_AGENT_PROVIDER=ollama for local
python -m cli chat
python -m cli ping                # prove the brain answers (prints latency)
```

Set several and SURA builds an automatic **fallback chain** (NVIDIA → Groq → Gemini). Details: [SYSTEM.md](SYSTEM.md).

### Enable voice — wake word + "only my voice" (free, offline)

```bash
pip install -r requirements-voice.txt    # faster-whisper STT (offline, accurate)
python -m cli enroll-voice               # 3 samples → SURA builds your voiceprint

python -m cli listen                     # SURA waits for "hey sura …"
python -m cli set-stt-model small.en     # optional: max accuracy for accented speech
```

Say *"hey SURA, open notepad"* and it chirps, answers **out loud** and runs it
on the PC. Every spoken turn — yours and SURA's — also lands on the chat screen
in the web UI, so a voice exchange can be read later and the same conversation
can continue by typing.

Voice mode survives crashes and reboots (`--with-voice` supervises the
listener exactly like the server, so *"hey SURA"* comes back on its own):

```bash
python -m cli autostart --share --with-voice   # mic live whenever you're logged in
```

Worth knowing: the mic is open while you are logged in (drop `--with-voice` to
stop that), the listener logs to `logs/voice-autostart.log`, and SURA does not
listen while it is speaking — let a sentence finish before the next wake word
(true barge-in is on the roadmap).

### Control SURA from your phone (same Wi-Fi)

```bash
python -m cli serve --share --qr    # prints the phone URL + a scan-to-pair QR
# (QR needs: pip install segno — optional)
```

SURA binds to your LAN, generates a short **pairing key**, and only devices
holding it can connect — your PC itself never needs the key. On the phone,
scan the QR (or type the URL + key once), then in Chrome use
*"Add to Home screen"* to install SURA as an app with bottom-tab navigation.
To survive reboots: `python -m cli autostart --share` — the pairing key is
saved to `config/agent_config.yaml` so your phone stays paired. The launcher
starts SURA hidden at every login and **keeps it online** (`cli supervise`
restarts the server if it ever stops), so the app window you open is already
live — nothing to run by hand. Logs: `logs/serve-autostart.log` (SURA's own
output) and `logs/serve-launcher.log` (anything that fails before SURA
starts). While
shared, the API-key box only works from the PC, and repeated wrong keys
trigger a five-minute lockout.

### Control SURA from anywhere (not just home Wi-Fi)

```bash
winget install tailscale.tailscale     # once: free private mesh VPN (3+ devices)
python -m cli serve --share --tunnel --qr
```

Install Tailscale on the PC **and** your phone, sign in to the same account
on both, and keep `serve --share` running. SURA then answers at a stable
address — `http://<machine-name>:8756` — from **any network**: mobile data,
college Wi-Fi, a friend's house. The tailnet is private (only your own
devices can join it) and the pairing key is enforced exactly as on the LAN;
unknown devices get a 401 and a lockout. Anywhere access survives reboots
with `python -m cli autostart --share --tunnel`.

**Installable app (HTTPS).** Chrome refuses to install PWAs from plain
`http://` pages ("this app cannot be installed"), so give SURA a real
certificate with Tailscale Serve — a one-time enable on the admin console
(the `tailscale serve` command prints the link), then:

```bash
tailscale serve --bg 8756   # https://<machine>.<tailnet>.ts.net → SURA
```

Install from `https://<machine>.<tailnet>.ts.net/?key=<pairing key>`
(Chrome menu → *Install app* / *Add to Home screen*). The proxy forwards
the client's real address in `X-Forwarded-For`, so the pairing gate stays
enforced over HTTPS too — no key is still a 401. The serve config
persists across reboots alongside Tailscale.

### Flick notes: laptop → phone

```bash
python -m cli push "https://youtube.com/... — remember this for the next video"
```

Every paired device pops it up as a toast (a system notification once you
allow the `🔔 notifications` prompt). From the web UI, use the gold
**📲 to phone** button in the header.

### Drive the laptop from your phone

Chat from the phone already executes on the PC — tools run where SURA
lives, and the reply streams back over the tailnet. Three tools make it
explicit:

- **run_pc_command** — run a shell command on the laptop and get its output
  (list files, check a download, git status, control media). Destructive
  patterns (format, del /s, reg delete, shutdown, …) are refused in code.
- **open_app** — launch a program or URL on the PC: "open chrome",
  "open spotify", "open E:\\videos".
- **push_to_phone** — SURA flicks its own notes and links back to your
  phone (same inbox as `python -m cli push`).

Example from the phone: *"open notepad, then tell me what's in
E:\\thumbnails"* → the app opens and the listing streams back.

### Why shared mode felt slow — and what changed

The pairing key was never the bottleneck (checking it is one 8-character
comparison). The delay was waiting for the *entire* LLM reply before
anything appeared. Replies now **stream in live** — first words in ~1–2 s
instead of after the whole generation. Two more levers:

- **Add a second provider key** (free): `python -m cli set-key groq <key>`
  from [console.groq.com](https://console.groq.com). SURA keeps a fallback
  chain (NVIDIA → Groq → Gemini) and switches automatically when one is
  slow or out of quota; Groq is typically much faster than NVIDIA NIM.
  Multiple keys from *different* providers help — duplicate keys from the
  same provider change nothing.
- **Cold starts**: NVIDIA's free tier can take tens of seconds on the first
  request after a long idle; any request (even `python -m cli ping`) warms
  it up again.

Every activation is verified against your voiceprint; anyone else who says
the wake word is refused out loud and logged. The accept threshold is
`0.90` by default — if *your* commands get rejected with low scores, the
print was recorded under different conditions than daily use: set
`voice_threshold: 0.75` (say) in `config/agent_config.yaml`, and re-record
hands-free with `python logs/voice_recapture.py` while sitting the way you
actually game — it re-enrolls and calibrates the threshold from your live
score.

## What SURA can do today

- **Converse** by text (CLI, web UI) or voice, with conversation context
- **Run the 13-step thumbnail workflow** end to end and write a review package
- **Remember**: searchable memory with cross-links; your corrections build confidence-scored rules (one correction is a tendency, not a law)
- **Check itself**: technical QA on real image files; perceptual QA surfaced as honest review checklists — it never claims verification it can't observe
- **Refuse safely**: approval-tier actions (publish, upload, send, spend, delete) return "needs approval" — through the LLM or not

Full capability map and the tier-by-tier growth plan: [ROADMAP.md](ROADMAP.md).

## Project structure

```
AGENT_SPEC.md            master specification (the constitution)
BEHAVIOR_RULES.md        how SURA must behave
PERMISSIONS.md           what requires approval — enforced by code
USER_PROFILE.md          Subhra's profile (SURA reads this)
MEMORY.md                memory design
ROADMAP.md               the E.D.I.T.H.-tier growth plan
SYSTEM.md                implementation guide (architecture ↔ code map)

system/                  the agent core
  orchestrator.py        core loop + routing + chat
  sqlite_memory.py       default memory store (search, links, history)
  permissions.py         permission gate
  brands.py              brand registry + separation enforcement
  llm/                   brain: client, agent loop, tools, system prompt
  voice/                 wake word, speaker verification, STT/TTS, loop
  server.py              local web-UI server (stdlib only)
  agents/creative/       Thumbnail Agent (brief, concepts, face library, QA)
cli/                     command-line interface (chat, serve, thumbnail, listen, …)
ui/                      web UI (vanilla HTML/CSS/JS)
tests/                   105 hermetic tests
brands/                  Subhra Gaming identity
face-library/            real face photos by expression (input needed)
thumbnail-system/        rules, references, generated packages
memory/                  SQLite store (gitignored)
```

## Testing

```bash
python -m unittest discover -s tests     # 105 tests, no hardware or keys required
```

## Privacy & safety

- Runs **locally**; the only external calls are to the LLM provider you configure
- Voiceprint (`voice/voiceprint.json`), memory DB, config keys, and logs are gitignored
- Approval-required actions are never executed autonomously — by code, not by prompt
- Logs are structured JSON with no secrets

## Docs

- [SYSTEM.md](SYSTEM.md) — architecture ↔ code map, setup guides
- [ROADMAP.md](ROADMAP.md) — the five-tier plan from tool user to operator
- [AGENT_SPEC.md](AGENT_SPEC.md) · [BEHAVIOR_RULES.md](BEHAVIOR_RULES.md) · [PERMISSIONS.md](PERMISSIONS.md) · [MEMORY.md](MEMORY.md) — the original specification (still the source of truth)

---

**Status:** core + LLM brain + SQLite memory + web UI + voice mode built and tested. Next up per the roadmap: image rendering for thumbnails, YouTube/GitHub connectors, proactive briefings. The things only Subhra can provide: `USER_PROFILE.md`, face photos, brand visual identities, and the LLM key of choice.
