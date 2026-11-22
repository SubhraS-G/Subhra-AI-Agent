"""Thumbnail concept generation.

COMPOSITION.md: "Generate 3-5 concepts before committing when the task
benefits from exploration." Concepts are compositions described in text —
the rendering step turns the chosen concept into an actual image.
"""

from __future__ import annotations

from dataclasses import dataclass

from system.agents.creative.brief import ThumbnailBrief

# Composition patterns from COMPOSITION.md. Not fixed templates — the
# generator adapts them to the actual video topic.
PATTERNS = [
    "creator face + main game element",
    "large game element + creator reaction",
    "cinematic gameplay scene + creator",
    "before/after",
    "challenge/result",
    "character transformation",
]


@dataclass
class Concept:
    pattern: str
    headline: str
    composition: str
    focal_point: str
    expression: str
    text_overlay: str
    rationale: str
    score: float = 0.0

    def describe(self) -> str:
        return (
            f"[{self.pattern}] {self.headline}\n"
            f"    Composition: {self.composition}\n"
            f"    Focal point: {self.focal_point}\n"
            f"    Expression:  {self.expression}\n"
            f"    Text:        {self.text_overlay or '(none)'}\n"
            f"    Why:         {self.rationale}"
        )


class ConceptGenerator:
    """Produces and ranks 3-5 thumbnail concepts from a brief."""

    def generate(self, brief: ThumbnailBrief, count: int = 4) -> list[Concept]:
        count = max(3, min(5, count))
        topic = brief.video_topic.strip()
        tone = brief.emotional_tone
        concepts: list[Concept] = []

        pattern_plan = self._pick_patterns(brief, count)
        for pattern in pattern_plan:
            concepts.append(self._build(brief, pattern, topic, tone))

        # Rank: prefer concepts that fit the requested pattern and content type.
        for concept in concepts:
            score = 1.0
            if brief.concept_pattern and brief.concept_pattern.lower() in concept.pattern.lower():
                score += 2.0
            if brief.text_overlay and concept.text_overlay == brief.text_overlay:
                score += 0.5
            if brief.content_type == "gaming" and "game" in concept.pattern:
                score += 0.5
            concept.score = score
        return sorted(concepts, key=lambda c: c.score, reverse=True)

    # ---------------------------------------------------------------- helpers

    @staticmethod
    def _pick_patterns(brief: ThumbnailBrief, count: int) -> list[str]:
        if brief.concept_pattern:
            matches = [p for p in PATTERNS if brief.concept_pattern.lower() in p.lower()]
            preferred = matches[0] if matches else brief.concept_pattern
            others = [p for p in PATTERNS if p != preferred]
            return [preferred] + others[: count - 1]
        return PATTERNS[:count]

    def _build(self, brief: ThumbnailBrief, pattern: str, topic: str, tone: str) -> Concept:
        overlay = brief.text_overlay
        if pattern == "creator face + main game element":
            return Concept(
                pattern=pattern,
                headline=f"{topic} — creator leads the frame",
                composition=(
                    "Creator occupies the left third with a strong key-lit edge; "
                    "the main game element fills the right two-thirds with directional motion toward the face."
                ),
                focal_point="Creator's eyes and the game element's leading edge",
                expression=tone,
                text_overlay=overlay,
                rationale="Highest identity recognition; keeps the game readable behind the reaction.",
            )
        if pattern == "large game element + creator reaction":
            return Concept(
                pattern=pattern,
                headline=f"{topic} — the element is the hero",
                composition=(
                    "The key game element dominates center-right at large scale; the creator reacts from the "
                    "bottom-left corner, smaller but fully recognizable, gaze pointed at the element."
                ),
                focal_point="The game element, with the reaction as secondary anchor",
                expression=tone,
                text_overlay=overlay,
                rationale="Element scale communicates the video's subject before any text is read.",
            )
        if pattern == "cinematic gameplay scene + creator":
            return Concept(
                pattern=pattern,
                headline=f"{topic} — cinematic moment",
                composition=(
                    "A cinematic in-game moment fills the frame; the creator is integrated into the environment "
                    "lighting on the right edge, color-graded to match the scene."
                ),
                focal_point="The scene's center of action",
                expression=tone,
                text_overlay=overlay,
                rationale="Best for storytelling and dramatic topics; identity stays recognizable via placement discipline.",
            )
        if pattern == "before/after":
            return Concept(
                pattern=pattern,
                headline=f"{topic} — before vs after",
                composition=(
                    "Vertical split: left shows the starting state, right the transformed state; the creator "
                    "bridges the split at the seam with a toned reaction."
                ),
                focal_point="The contrast seam",
                expression=tone,
                text_overlay=overlay or "BEFORE / AFTER",
                rationale="Transformation is instantly legible even at small scale.",
            )
        if pattern == "challenge/result":
            return Concept(
                pattern=pattern,
                headline=f"{topic} — challenge outcome",
                composition=(
                    "The challenge setup in the background; the result (win/fail state) foregrounded with the "
                    "creator's reaction confirming the outcome."
                ),
                focal_point="The result state",
                expression=tone,
                text_overlay=overlay or "RESULT",
                rationale="Outcome-driven curiosity gap; strong at small scale when the result is visual.",
            )
        # character transformation (and any custom pattern)
        return Concept(
            pattern=pattern,
            headline=f"{topic} — transformation concept",
            composition=(
                "Subject transformation across the frame with the creator anchoring one side; "
                "clear stage-by-stage visual progression."
            ),
            focal_point="The most transformed subject stage",
            expression=tone,
            text_overlay=overlay,
            rationale="Transformation reads instantly when stage differences are exaggerated cleanly.",
        )
