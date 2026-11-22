# ROADMAP — SUBHRA AI AGENT → E.D.I.T.H. TIER

Target: a J.A.R.V.I.S. / F.R.I.D.A.Y. / E.D.I.T.H.-class personal assistant —
always available, speaks, sees, remembers everything, acts proactively across
Subhra's creator work, game dev, and machines — with Stark-grade judgment
about what it is allowed to do without asking.

Current state (v0.1.0): orchestrator core loop, file-based memory with
confidence learning, permissions gate, brand separation, Thumbnail Agent,
CLI, tests. The skeleton is right; the missing tiers are below.

---

## The five tiers

```
T1  TOOL USER        does things when asked                     ← we are here (partially)
T2  CONNECTED        sees and touches the outside world
T3  AWARE            remembers, listens, watches continuously
T4  PROACTIVE        brings you things you didn't ask for
T5  OPERATOR         runs your workflows end-to-end, supervised
```

---

## TIER 1 — finish the foundation (weeks 1–2)

Small effort, everything else depends on it.

- [ ] Fill `USER_PROFILE.md` completely — identity, goals, gear, schedule, preferences
- [ ] Add real face photos to all 8 expression folders in `face-library/`
- [ ] Fill both `brands/*/VISUAL_IDENTITY.md` files
- [ ] Collect 10–20 liked/disliked thumbnails in `thumbnail-system/references/`
- [ ] Swap file-based memory for a local database (SQLite is enough) with the same
      interface — memory needs search, timestamps, and cross-linking to scale

**Subhra does:** profiles, photos, brand docs. That data is impossible to fake.

---

## TIER 2 — connect the outside world (weeks 2–6)

The agent is currently blind and isolated. Give it eyes and hands.

### 2a. LLM brain (the single biggest upgrade)
- [ ] Wire an LLM into the orchestrator (`UNDERSTAND`, `PLAN`, concept writing)
      via API (Claude/GPT/Gemini) or a local model (Ollama) if privacy matters
- [ ] Add function/tool calling so the brain can invoke the existing agents
- [ ] System prompt generated from `core_docs.py` — the MD files become its personality

### 2b. YouTube + GitHub integration
- [ ] YouTube Data API: draft titles/descriptions/tags, fetch analytics, check comments
- [ ] GitHub API: issue triage, PR summaries, commit reports (push stays gated)
- [ ] Research agent with web search, verified sources, saved briefs

### 2c. Image rendering
- [ ] Connect an image tool to thumbnail step 8 (local: ComfyUI/SDXL+ControlNet
      using real face references; or an API) — ControlNet/IP-Adapter keeps
      facial identity, which is your hard rule
- [ ] Automated visual QA with a vision model against `QA_RULES.md`

**Subhra does:** creates API keys, decides cloud-vs-local, approves the spend.

---

## TIER 3 — awareness (months 2–4)

J.A.R.V.I.S. is always present. This tier makes the agent ambient.

- [x] **Voice**: wake word ("hey sura") + offline STT + TTS (pyttsx3) +
      speaker verification so it answers only to Subhra's voice —
      BUILT (system/voice/, free, offline). Verified live end to end: fuzzy
      wake matching (`"hey sir"`, `"hey so"` all wake SURA), spoken replies,
      and a chat-screen mirror of every spoken turn. `autostart --with-voice`
      keeps the listener alive. Remaining: barge-in interruption, streaming
      replies, far-field mic tuning.
- [ ] **Always-on memory**: every conversation, decision, and correction lands in
      the database automatically; session summaries written nightly
- [ ] **Screen/computer use**: local vision model reads your screen when asked
      ("what's wrong with this error") — the F.R.I.D.A.Y. "look at this" moment
- [ ] **Notifications in**: Discord/phone messages reach the agent; it can answer
      on your behalf in draft mode
- [ ] **Context engine**: before any task, it recalls relevant memory, projects,
      and past corrections automatically

**Subhra does:** buys/assigns a mic + always-on machine (a spare PC or the
game server), decides privacy boundaries for screen data.

---

## TIER 4 — proactive operation (months 4–8)

The agent starts bringing things to *you*.

- [x] **Video-editing assist (v1, BUILT)**: paste study notes/links about
      editing styles you admire -> SURA distills style profiles (pacing,
      cuts, zooms, sound design) and generates per-video edit plans
      (`editing/plans/`), plus chat tools `learn_editing_style` and
      `build_edit_plan`. This is the "5% like them" assist — real video
      analysis comes later.
- [x] **Taskbar app + auto-start (BUILT)**: the web UI is an installable
      PWA (Chrome > "Install page as app" pins it to the taskbar), and
      `python -m cli autostart` starts the server hidden at every login and
      keeps it online (`cli supervise` relaunches it after a crash, and only
      ever one instance runs); `--with-voice` supervises the wake-word
      listener the same way. The app window is live from the moment it opens.
- [ ] **Morning briefing**: analytics deltas, subs/comments, GitHub events,
      today's plan — delivered by voice or message
- [ ] **Trend watch**: gaming/keyword monitoring that suggests video ideas with
      thumbnail concepts attached
- [ ] **Autonomous pre-work**: when you announce a video, it drafts title,
      description, tags, thumbnail package, and short cuts *before* you ask
- [ ] **Initiative budget** (Stark judgment, formalized): the agent self-executes
      anything reversible + low-risk, asks for anything consequential —
      encoded in the existing permissions gate as per-context tiers
- [ ] **Multi-machine reach**: runs tasks on your dev PC / server via a secure
      local agent protocol

**Subhra does:** defines the initiative budget rules explicitly, reviews the
first month of proactive suggestions daily, then weekly.

---

## TIER 5 — full operator (months 8+)

- [ ] End-to-end video pipeline: idea → research → script → thumbnail →
      upload draft → publish (publish stays approval-gated, always)
- [ ] Game-dev copilot: it plans, implements, tests, and reports on your
game projects (GTA V Labs is paused; this lane reactivates when you bring it back)
- [ ] Self-improvement loop: weekly self-review of corrections and failures,
      proposes its own rule changes for your approval
- [ ] Long-horizon planning: monthly content calendars, monetization tracking,
      goal progress against `USER_PROFILE.md`

**Subhra does:** supervision only — and quarterly audits of what it changed
without being asked.

---

## Non-negotiables at every tier

These are already in the code; keep them as capabilities grow:

1. **Permissions gate stays** — publishing, spending, deleting, external
   messages always require approval. E.D.I.T.H. asks before the missile.
2. **Verification before claims** — nothing is "done" unless verified.
3. **Brand separation** — automated check, hard failure on violation.
4. **Learning discipline** — one correction ≠ permanent rule.
5. **Local-first privacy** — face library, personal memory, and screen data
   stay on your machines; you decide what ever leaves.

## Honest constraint

Code gets you tiers 1–2 cheaply, but tiers 3–5 are gated by three real
inputs only you can provide: **API keys/budget** (or a GPU for local
models), **your data** (profile, faces, references, analytics access), and
**your trust** (the initiative budget). The engineering is the easy part.
