"""Thumbnail brief model.

Implements step 4 of the thumbnail workflow:

    User request -> channel identification -> thumbnail brief -> face selection -> ...

The brief is the structured contract between the orchestrator and the
thumbnail pipeline. Every downstream step (face selection, concepts,
QA) reads from the brief, never from the raw request.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ThumbnailBrief:
    """Structured thumbnail brief."""

    video_topic: str
    channel: str                 # brand slug, e.g. "subhra-gaming"
    emotional_tone: str = "excited"
    content_type: str = "gaming"  # gaming | livestream | reaction | dramatic
    text_overlay: str = ""        # short phrase only; never the full title
    must_include: list[str] = field(default_factory=list)
    must_avoid: list[str] = field(default_factory=list)
    concept_pattern: str = ""     # e.g. "creator face + main game element"
    notes: str = ""

    def to_markdown(self) -> str:
        lines = [
            "# THUMBNAIL BRIEF",
            "",
            f"- Video topic: {self.video_topic}",
            f"- Channel: {self.channel}",
            f"- Emotional tone: {self.emotional_tone}",
            f"- Content type: {self.content_type}",
            f"- Text overlay: {self.text_overlay or '(none)'}",
        ]
        if self.must_include:
            lines.append(f"- Must include: {', '.join(self.must_include)}")
        if self.must_avoid:
            lines.append(f"- Must avoid: {', '.join(self.must_avoid)}")
        if self.concept_pattern:
            lines.append(f"- Concept pattern: {self.concept_pattern}")
        if self.notes:
            lines.append(f"- Notes: {self.notes}")
        return "\n".join(lines)

    def validate(self) -> list[str]:
        """Return a list of validation problems (empty when valid)."""
        problems: list[str] = []
        if not self.video_topic.strip():
            problems.append("video_topic is required")
        if not self.channel.strip():
            problems.append("channel is required (brand separation rule)")
        if self.text_overlay and len(self.text_overlay) > 30:
            problems.append(
                f"text_overlay is {len(self.text_overlay)} chars; prefer short phrases "
                "(thumbnail rules: do not duplicate the full video title)"
            )
        return problems
