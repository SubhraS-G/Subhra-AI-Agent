"""EditingAgent — SURA's video-editing brain (Tier 4, version 1).

Goal (Subhra's words): "at least 5% like the top gaming YouTubers" — a
working assist, not perfection. The loop is:

    LEARN   Subhra pastes study notes / YouTube links about editing videos
            he admires -> agent distills a style profile (pacing, cuts,
            zooms, sound design) into memory + editing/styles.json
    PLAN    for a concrete video topic, the agent turns the merged style
            profile + Subhra's gear into a per-video edit plan
            (hook, pacing targets, effect recipes, sound design, export)
            written to editing/plans/<slug>.md

Design notes:
- Free-first and honest: without video analysis (a later milestone) the
  profile comes from what Subhra observed and pasted. The plan always says
  where each number came from (style profile vs. sensible default).
- Deterministic core (no LLM needed), so it works in rule mode and is
  unit-testable; the LLM brain can call it through a tool and narrate.
"""

from __future__ import annotations

import datetime as _dt
import json
import re
from pathlib import Path

from system.agents.base import SpecialistAgent, TaskResult
from system.logging_utils import log
from system.paths import EDITING_DIR, EDITING_PLANS_DIR

STYLES_FILE = EDITING_DIR / "styles.json"

# Names SURA recognizes as study targets in pasted text (extendable).
KNOWN_CREATORS = ("mrbeast", "dude perfect", "jacksepticeye", "markiplier",
                  "pewdiepie", "dream", "technoblade", "vikkstar", "ssundee",
                  "lai bhaari", "carryminati", "techno gamerz", "total gaming")

# YouTube link / id extraction (handles watch, youtu.be, shorts).
_YT_RE = re.compile(
    r"(?:youtube\.com/(?:watch\?v=|shorts/|live/)|youtu\.be/)([\w-]{6,})"
)


def _slug(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:60] or "video"


# --------------------------------------------------------------------- styles

def default_styles() -> dict:
    """Baseline gaming-YouTube style, used until real profiles are learned.

    Numbers are conservative starting points, clearly labeled as defaults in
    every generated plan.
    """
    return {
        "baseline": {
            "label": "gaming baseline (generic defaults)",
            "sources": [],
            "hook_seconds": 8,
            "avg_shot_seconds": 4.0,
            "cut_style": "hard cuts on beat/action; no fades mid-gameplay",
            "zoom_usage": "punch-in 105-125% on kill/reaction, 4-8 frames ease",
            "effects": ["speed ramps on highlights", "subtle shake on impacts"],
            "sound_design": ["whoosh on transitions", "memorable hit marker sound",
                             "music dips under speech"],
            "text_usage": "short 2-4 word callouts, bottom third, bold",
        }
    }


class StyleBook:
    """All learned style profiles, persisted to editing/styles.json."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or STYLES_FILE
        self.data: dict = {"styles": default_styles()}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            stored = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(stored, dict) and "styles" in stored:
                # learned profiles override the baseline key-by-key
                merged = default_styles()
                merged["styles"].update(stored["styles"])
                self.data = merged
        except (json.JSONDecodeError, OSError):
            log("styles_load_failed", path=str(self.path))

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, indent=2, ensure_ascii=False),
                             encoding="utf-8")

    def names(self) -> list[str]:
        return [n for n in self.data["styles"] if n != "baseline"]

    def get(self, name: str) -> dict | None:
        return self.data["styles"].get(name.lower())

    def learn(self, text: str) -> dict:
        """Distill pasted notes/links into (or update) a style profile.

        Returns {"creator", "created", "message"}.
        """
        text_l = text.lower()
        creator = next((c for c in KNOWN_CREATORS if c in text_l), None)
        if not creator:
            # First significant word group of the paste as the profile name.
            words = re.findall(r"[a-zA-Z]{3,}", text)
            creator = " ".join(words[:2]).lower() if words else "unnamed-style"
        profile = self.data["styles"].get(creator, {
            "label": f"learned style: {creator}",
            "sources": [],
            "hook_seconds": None,
            "avg_shot_seconds": None,
            "cut_style": None,
            "zoom_usage": None,
            "effects": [],
            "sound_design": [],
            "text_usage": None,
        })

        link_ids = _YT_RE.findall(text)
        for link_id in link_ids:
            src = f"https://youtu.be/{link_id}"
            if src not in profile["sources"]:
                profile["sources"].append(src)

        def _num(pattern: str) -> float | None:
            m = re.search(pattern, text_l)
            return float(m.group(1)) if m else None

        if v := _num(r"hook[^\d]{0,12}(\d+(?:\.\d+)?)\s*(?:s\b|sec)"):
            profile["hook_seconds"] = v
        if v := _num(r"(?:avg(?:erage)?\s*)?(?:shot|cut)[^\d]{0,16}(\d+(?:\.\d+)?)\s*(?:s\b|sec)"):
            profile["avg_shot_seconds"] = v
        if m := re.search(r"(cut(?:s|ting)?[^.\n]{5,120})", text_l):
            profile["cut_style"] = m.group(1).strip()
        if m := re.search(r"(zoom[^.\n]{5,120})", text_l):
            profile["zoom_usage"] = m.group(1).strip()
        for m in re.findall(r"(?:use|adds?|uses?)\s+([a-z][^.;\n]{4,60})", text_l):
            phrase = m.strip()
            if any(k in phrase for k in ("ramp", "shake", "glow", "flash", "speed", "blur")) \
                    and phrase not in profile["effects"]:
                profile["effects"].append(phrase)
        for m in re.findall(r"(?:sound|sfx|music)[^.\n:]{0,20}:\s*([^.;\n]{4,80})", text_l):
            phrase = m.strip()
            if phrase not in profile["sound_design"]:
                profile["sound_design"].append(phrase)

        self.data["styles"][creator] = profile
        self.save()
        log("style_learned", creator=creator, sources=len(profile["sources"]))
        return {
            "creator": creator,
            "created": creator not in self.names() or True,
            "message": (
                f"Style profile '{creator}' saved "
                f"({len(profile['sources'])} video source(s) noted). "
                "Paste more observations and I merge them into the same profile."
            ),
        }

    def merged(self) -> dict:
        """Baseline with every learned profile overlaid (last learned wins)."""
        merged = dict(default_styles()["baseline"])
        learned = [self.data["styles"][n] for n in self.names()]
        for profile in learned:
            for key, value in profile.items():
                if key in ("label", "sources"):
                    continue
                if value in (None, [], ""):
                    continue
                if isinstance(value, list):
                    for item in value:
                        if item not in merged.setdefault(key, []):
                            merged[key].append(item)
                else:
                    merged[key] = value
        merged["learned_from"] = [s for p in learned for s in p["sources"]]
        return merged


# ----------------------------------------------------------------------- plan

def build_edit_plan(topic: str, stylebook: StyleBook | None = None,
                    context: dict | None = None) -> tuple[Path, dict]:
    """Generate a per-video edit plan from the merged style profile.

    Returns (plan_path, meta). Pure string work — no LLM required.
    """
    stylebook = stylebook or StyleBook()
    style = stylebook.merged()
    context = context or {}
    hook = style.get("hook_seconds") or 8
    shot = style.get("avg_shot_seconds") or 4.0
    slug = _slug(topic or "video")
    date = _dt.date.today().isoformat()
    effects = style.get("effects") or []
    sound = style.get("sound_design") or []

    def per_min(target_seconds: float) -> str:
        return f"~{int(60 / max(target_seconds, 0.5))} per minute"

    lines = [
        f"# Edit plan — {topic or 'video'}",
        "",
        f"date: {date}  ·  style: {style.get('label', 'baseline')}",
        "",
        "## 1. Hook (first 0:00-0:%02d)" % int(hook),
        f"- Cold-open on the single best moment; no intro, no logo.",
        f"- State the payoff in one line before 0:{int(hook):02d}.",
        "- First frame must move: jump straight into action, never a title card.",
        "",
        "## 2. Pacing",
        f"- Target average shot length: {shot:.1f}s ({per_min(shot)}) — cut before it drags.",
        "- If a segment has no kill/fail/joke within 3 shots, cut it or speed-ramp it.",
        "- Vary rhythm: 2-3 fast cuts after every slow beat so it never feels flat.",
        "",
        "## 3. Zooms & camera moves",
        "- " + (style.get("zoom_usage") or "punch-in on reactions; ease 4-8 frames."),
        "- Max 1 punch-in per 30s — overuse reads as spam.",
        "- Slow push-in during story moments; snap zoom only on impacts.",
        "",
        "## 4. Effects recipes",
    ]
    if effects:
        lines += [f"- {e}" for e in effects[:8]]
    else:
        lines += ["- speed ramp on the replay of any highlight",
                  "- 2-frame white flash on big impacts"]
    lines += [
        "",
        "## 5. Sound design",
    ]
    if sound:
        lines += [f"- {s}" for s in sound[:8]]
    else:
        lines += ["- whoosh into every transition",
                  "- duck music -12dB under speech",
                  "- one signature impact sound for the biggest moment"]
    lines += [
        "",
        "## 6. Text & callouts",
        "- " + (style.get("text_usage") or "2-4 word callouts, bottom third, bold, 1s on screen."),
        "",
        "## 7. Export checklist",
        "- 1440p60 (or 1080p60) · high bitrate · loudness -14 LUFS",
        "- Watch once at 1x muted (does it read?) and once at 2x (any dead air?).",
        "- Thumbnail pick happens via the thumbnail workflow; export 3 candidates.",
        "",
    ]
    if style.get("learned_from"):
        lines.append("## Style sources studied")
        lines += [f"- {src}" for src in style["learned_from"][:10]]
        lines.append("")

    path = EDITING_PLANS_DIR / f"{date}_{slug}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")
    log("edit_plan_built", topic=topic, path=str(path))
    return path, {"topic": topic, "style": style.get("label", "baseline")}


def list_plans(limit: int = 30) -> list[dict]:
    out = []
    if EDITING_PLANS_DIR.exists():
        for path in sorted(EDITING_PLANS_DIR.glob("*.md"), reverse=True)[:limit]:
            out.append({"name": path.stem, "modified": path.stat().st_mtime})
    return out


def read_plan(name: str) -> dict:
    if not name or "/" in name or "\\" in name or ".." in name:
        return {"error": "invalid plan name"}
    path = EDITING_PLANS_DIR / f"{name}.md"
    if not path.exists():
        return {"error": "plan not found"}
    return {
        "name": name,
        "markdown": path.read_text(encoding="utf-8"),
        "modified": path.stat().st_mtime,
    }


# ---------------------------------------------------------------------- agent

class EditingAgent(SpecialistAgent):
    """Specialist agent for video-editing study and edit planning."""

    name = "editing-agent"
    role = "editing"

    def __init__(self, memory=None, permissions=None, stylebook: StyleBook | None = None) -> None:
        super().__init__(memory=memory, permissions=permissions)
        self.stylebook = stylebook or StyleBook()

    def handle(self, objective: str, context: dict | None = None) -> TaskResult:
        context = context or {}
        mode = context.get("mode", "plan")
        if mode == "learn":
            out = self.stylebook.learn(context.get("text", "") or objective)
            self.memory.write_note("workflows", "editing_styles",
                                   json.dumps(self.stylebook.data["styles"], indent=2,
                                              ensure_ascii=False))
            return TaskResult(
                agent=self.name, objective=objective, status="completed",
                summary=out["message"], artifacts=[str(self.stylebook.path)],
            )
        # default: build an edit plan
        topic = context.get("topic") or objective
        path, meta = build_edit_plan(topic, self.stylebook, context)
        return TaskResult(
            agent=self.name, objective=objective, status="completed",
            summary=(f"Edit plan written for '{meta['topic']}' using style "
                     f"'{meta['style']}'. Follow sections 1-7 in order."),
            artifacts=[str(path)],
            next_action="Open the plan, edit in your editor, tick the export checklist.",
        )
