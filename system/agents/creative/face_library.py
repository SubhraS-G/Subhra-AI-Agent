"""Face library access.

AGENT_SPEC section 7: Subhra provides real photographs organized by
expression; the agent selects references based on the video's topic and
emotional tone. This module indexes the library and performs selection.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from system import paths

EXPRESSIONS = [
    "neutral", "happy", "excited", "shocked",
    "angry", "confused", "scared", "serious",
]


class FaceLibraryError(Exception):
    pass


@dataclass
class FaceReference:
    path: Path
    expression: str
    angle: str = ""
    lighting: str = ""
    crop: str = ""
    suitability: dict = field(default_factory=dict)

    def score_for(self, tone: str, content_type: str) -> float:
        """Score how well this reference fits a tone/content combination."""
        score = 0.0
        if self.suitability:
            score += float(self.suitability.get(content_type, 0.5))
            score += 0.5 * float(self.suitability.get("reaction", 0.5))
        else:
            score += 0.5
        # Exact expression match beats partial relevance.
        if self.expression == tone:
            score += 2.0
        elif tone and tone in self.expression or self.expression in tone:
            score += 1.0
        return score


class FaceLibrary:
    """Indexes face-library/ and selects references by expression."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = root or paths.FACE_LIBRARY
        self._refs: list[FaceReference] | None = None

    # ------------------------------------------------------------------ index

    def index(self) -> list[FaceReference]:
        if self._refs is not None:
            return self._refs
        refs: list[FaceReference] = []
        meta = self._parse_metadata()
        if meta:
            refs.extend(meta)
        else:
            # Fall back to directory structure: face-library/<expression>/*
            for expression in EXPRESSIONS:
                folder = self.root / expression
                if folder.is_dir():
                    for image in sorted(folder.iterdir()):
                        if image.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}:
                            refs.append(FaceReference(path=image, expression=expression))
        self._refs = refs
        return refs

    def _parse_metadata(self) -> list[FaceReference]:
        if not paths.FACE_METADATA.exists():
            return []
        text = paths.FACE_METADATA.read_text(encoding="utf-8")
        # Only parse real entries, not the commented example.
        active = []
        for line in text.splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("#") and stripped.startswith("- id:"):
                active.append(line)
        if not active:
            return []
        return self._parse_yaml_entries(text)

    def _parse_yaml_entries(self, text: str) -> list[FaceReference]:
        """Minimal YAML parsing for the documented metadata schema."""
        refs = []
        current = None
        for raw in text.splitlines():
            if not raw.strip() or raw.strip().startswith("#"):
                continue
            indent = len(raw) - len(raw.lstrip())
            stripped = raw.strip()
            if stripped.startswith("- id:") and indent == 0:
                if current:
                    refs.append(current)
                current = FaceReference(path=Path(stripped.split(":", 1)[1].strip()), expression="")
                continue
            if current is None:
                continue
            if indent == 2 and ":" in stripped:
                key, _, value = stripped.partition(":")
                key, value = key.strip(), value.strip()
                if key == "filename":
                    current.path = self.root / value
                elif key == "expression":
                    current.expression = value
                elif key in {"angle", "lighting", "crop"}:
                    setattr(current, key, value)
            elif stripped.startswith("suitability:") is False and indent >= 4 and ":" in stripped:
                key, _, value = stripped.partition(":")
                if key.strip() in {"gaming", "livestream", "reaction", "dramatic"}:
                    try:
                        current.suitability[key.strip()] = float(value.strip())
                    except ValueError:
                        pass
        if current:
            refs.append(current)
        return refs

    # -------------------------------------------------------------- selection

    def available_expressions(self) -> list[str]:
        return sorted({r.expression for r in self.index() if r.expression})

    def select(self, tone: str, content_type: str = "gaming", limit: int = 3) -> list[FaceReference]:
        """Return the best-matching face references for a tone, best first."""
        refs = self.index()
        if not refs:
            raise FaceLibraryError(
                "Face library is empty. Populate face-library/<expression>/ with real photographs "
                "before requesting thumbnails that include the creator."
            )
        tone_key = tone.strip().lower()
        ranked = sorted(
            refs,
            key=lambda r: r.score_for(tone_key, content_type),
            reverse=True,
        )
        return ranked[:limit]

    def status(self) -> dict:
        refs = self.index()
        by_expression: dict[str, int] = {}
        for ref in refs:
            by_expression[ref.expression] = by_expression.get(ref.expression, 0) + 1
        return {
            "total": len(refs),
            "by_expression": by_expression,
            "missing_expressions": [e for e in EXPRESSIONS if e not in by_expression],
        }
