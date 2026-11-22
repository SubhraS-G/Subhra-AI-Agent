"""Memory system for SURA.

Implements MEMORY.md:

- Permanent memory  (memory/permanent/*.md)
- Project memory    (memory/projects/*.md)
- Workflow memory   (memory/workflows/*.md)
- Temporary memory  (memory/temporary/*.md)

Learning rules follow the documented progression:

    One correction -> temporary preference -> repeated correction
    -> candidate rule -> consistent behavior -> established preference

Each preference is stored as YAML front-matter + markdown body, with a
confidence level (unknown/low/medium/high/established) and evidence count.
A single correction never creates an established rule automatically.
"""

from __future__ import annotations

import datetime as _dt
import re
from dataclasses import dataclass
from pathlib import Path

from system import paths
from system.logging_utils import log

CONFIDENCE_ORDER = ["unknown", "low", "medium", "high", "established"]

# Number of consistent pieces of evidence required to promote a rule one level.
# A single correction (evidence 1) can never exceed "low".
PROMOTION_EVIDENCE = {
    "unknown": 1,
    "low": 2,
    "medium": 4,
    "high": 8,
}


@dataclass
class Preference:
    """A learned preference with confidence tracking."""

    id: str
    rule: str
    scope: str
    confidence: str
    evidence_count: int
    last_updated: str
    body: str = ""

    def to_markdown(self) -> str:
        lines = [
            "---",
            f"id: {self.id}",
            f"rule: \"{self.rule}\"",
            f"scope: {self.scope}",
            f"confidence: {self.confidence}",
            f"evidence_count: {self.evidence_count}",
            f"last_updated: {self.last_updated}",
            "---",
            "",
            self.body.strip(),
            "",
        ]
        return "\n".join(lines)

    @classmethod
    def from_markdown(cls, path: Path) -> "Preference":
        text = path.read_text(encoding="utf-8")
        meta = {}
        body = text
        if text.startswith("---"):
            end = text.find("---", 3)
            if end != -1:
                for line in text[3:end].splitlines():
                    if ":" in line:
                        key, _, value = line.partition(":")
                        meta[key.strip()] = value.strip().strip('"')
                body = text[end + 3 :]
        return cls(
            id=meta.get("id", path.stem),
            rule=meta.get("rule", ""),
            scope=meta.get("scope", "general"),
            confidence=meta.get("confidence", "unknown"),
            evidence_count=int(meta.get("evidence_count", "0") or 0),
            last_updated=meta.get("last_updated", ""),
            body=body.strip(),
        )


class MemoryStore:
    """File-backed memory across the four documented memory classes.

    `root` overrides the memory directory (used by tests to stay hermetic);
    by default the documented memory/ tree is used.
    """

    def __init__(self, root: Path | None = None) -> None:
        self.root = root
        if root is not None:
            for directory in (root / "permanent", root / "projects", root / "workflows", root / "temporary"):
                directory.mkdir(parents=True, exist_ok=True)
        else:
            paths.ensure_directories()

    def _base(self, memory_class: str) -> Path:
        if self.root is not None:
            return self.root / memory_class
        return {
            "permanent": paths.PERMANENT_MEMORY,
            "projects": paths.PROJECT_MEMORY,
            "workflows": paths.WORKFLOW_MEMORY,
            "temporary": paths.TEMPORARY_MEMORY,
        }[memory_class]

    # ------------------------------------------------------------------ read

    def list_notes(self, memory_class: str) -> list[Path]:
        return sorted(self._base(memory_class).glob("*.md"))

    def read_note(self, memory_class: str, name: str) -> str | None:
        path = self.note_path(memory_class, name)
        if path.exists():
            return path.read_text(encoding="utf-8")
        return None

    # ----------------------------------------------------------------- write

    def write_note(self, memory_class: str, name: str, content: str) -> Path:
        path = self.note_path(memory_class, name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content.rstrip() + "\n", encoding="utf-8")
        log("memory_write", memory_class=memory_class, note=name)
        return path

    def delete_temporary(self, name: str) -> bool:
        path = self._base("temporary") / f"{name}.md"
        if path.exists():
            path.unlink()
            log("memory_delete", memory_class="temporary", note=name)
            return True
        return False

    # ------------------------------------------------------------ preferences

    def list_preferences(self, scope: str | None = None) -> list[Preference]:
        prefs: list[Preference] = []
        for base in (self._base("permanent"), self._base("workflows")):
            for path in sorted(base.glob("pref_*.md")):
                pref = Preference.from_markdown(path)
                if scope is None or pref.scope == scope:
                    prefs.append(pref)
        return prefs

    def record_correction(
        self, rule: str, scope: str = "general", note: str = ""
    ) -> Preference:
        """Record one correction and apply the learning progression.

        Per MEMORY.md, a single correction is only a temporary preference; it
        becomes a candidate rule only after repeated corrections.
        """
        slug = re.sub(r"[^a-z0-9]+", "_", rule.lower()).strip("_")[:48]
        existing = None
        for pref in self.list_preferences(scope=scope):
            if pref.id == slug or pref.rule.lower() == rule.lower():
                existing = pref
                break

        today = _dt.date.today().isoformat()
        if existing is None:
            pref = Preference(
                id=slug,
                rule=rule,
                scope=scope,
                confidence="low",
                evidence_count=1,
                last_updated=today,
                body=note,
            )
        else:
            existing.evidence_count += 1
            existing.confidence = self._promote(existing.confidence, existing.evidence_count)
            existing.last_updated = today
            if note:
                existing.body = (existing.body + "\n\n" + note).strip()
            pref = existing

        base = (
            self._base("permanent")
            if pref.confidence == "established"
            else self._base("workflows")
        )
        path = base / f"pref_{pref.id}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(pref.to_markdown(), encoding="utf-8")
        log("preference_recorded", id=pref.id, confidence=pref.confidence, evidence=pref.evidence_count)
        return pref

    @staticmethod
    def _promote(current: str, evidence: int) -> str:
        if current == "established":
            return current
        target = "low"
        for level, needed in PROMOTION_EVIDENCE.items():
            if evidence >= needed:
                target = {
                    "low": "low",
                    "medium": "medium",
                    "high": "high",
                }.get(level, "low")
        # Map evidence thresholds directly.
        if evidence >= PROMOTION_EVIDENCE["high"]:
            target = "high"
        elif evidence >= PROMOTION_EVIDENCE["medium"]:
            target = "medium"
        elif evidence >= PROMOTION_EVIDENCE["low"]:
            target = "low"
        order = CONFIDENCE_ORDER
        return target if order.index(target) > order.index(current) else current

    # ------------------------------------------------------------- internals

    @staticmethod
    def _note_path(memory_class: str, name: str) -> Path:
        raise NotImplementedError("use instance method instead")

    def note_path(self, memory_class: str, name: str) -> Path:
        return self._base(memory_class) / f"{name}.md"
