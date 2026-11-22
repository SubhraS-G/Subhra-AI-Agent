"""Thumbnail Agent — the first specialist agent.

Executes workflows/thumbnails/THUMBNAIL_WORKFLOW.md end to end:

    1. Identify channel
    2. Understand video topic
    3. Determine emotional tone
    4. Create structured thumbnail brief
    5. Select face reference if needed
    6. Generate concepts
    7. Select promising concepts
    8. Generate/edit composition
    9. Integrate branding
    10. Run visual QA
    11. Produce variants
    12. Present for user review
    13. Record useful feedback

Rendering: when an image-generation tool is connected it plugs in at step 8.
Without one, the agent produces the full pre-render package (brief, face
selections, ranked concepts, layer-by-layer composition plan, brand
integration sheet for Subhra Gaming) and runs technical QA on any supplied render.
"""

from __future__ import annotations

import datetime as _dt
import json
import re
from pathlib import Path

from system.agents.base import SpecialistAgent, TaskResult
from system.agents.creative.brief import ThumbnailBrief
from system.agents.creative.concepts import Concept, ConceptGenerator
from system.agents.creative.face_library import FaceLibrary, FaceLibraryError
from system.agents.creative.qa import VisualQA, small_scale_summary
from system.brands import Brand, BrandRegistry, BrandError, load_brand_identity
from system.logging_utils import log

TONE_KEYWORDS = {
    "excited": ["excited", "hype", "insane", "crazy", "epic"],
    "shocked": ["shocked", "unbelievable", "wtf", "no way", "unexpected"],
    "happy": ["happy", "fun", "chill", "wholesome"],
    "angry": ["angry", "rage", "frustrat"],
    "confused": ["confused", "why", "how", "mystery"],
    "scared": ["scared", "horror", "creepy", "terrif"],
    "serious": ["serious", "guide", "tutorial", "review", "analysis"],
    "neutral": [],
}


class ThumbnailAgent(SpecialistAgent):
    name = "thumbnail-agent"
    role = "creative"

    def __init__(self, memory=None, permissions=None, face_library: FaceLibrary | None = None) -> None:
        super().__init__(memory, permissions)
        self.faces = face_library or FaceLibrary()
        self.brands = BrandRegistry()

    # ------------------------------------------------------------- entrypoint

    def handle(self, objective: str, context: dict | None = None) -> TaskResult:
        context = context or {}
        log("thumbnail_task_start", objective=objective)

        # Steps 1-3: channel, topic, tone
        request_text = objective + " " + json.dumps(context)
        try:
            if context.get("channel_override"):
                brand = self.brands.get(context["channel_override"])
            else:
                brand = self.brands.resolve(request_text)
            self.brands.assert_separation(request_text)
        except BrandError as exc:
            return TaskResult(
                agent=self.name, objective=objective, status="needs_input",
                summary=str(exc), next_action="Specify the channel (Subhra Gaming).",
            )

        topic = context.get("video_topic") or self._extract_topic(objective)
        tone = context.get("emotional_tone") or self._infer_tone(objective + " " + topic)
        content_type = context.get("content_type") or "gaming"

        # Step 4: structured brief
        brief = ThumbnailBrief(
            video_topic=topic,
            channel=brand.slug,
            emotional_tone=tone,
            content_type=content_type,
            text_overlay=context.get("text_overlay", ""),
            must_include=context.get("must_include", []),
            must_avoid=context.get("must_avoid", []),
            concept_pattern=context.get("concept_pattern", ""),
            notes=context.get("notes", ""),
        )
        problems = brief.validate()
        if problems:
            return TaskResult(
                agent=self.name, objective=objective, status="needs_input",
                summary="Brief invalid: " + "; ".join(problems),
                next_action="Provide the missing brief fields.",
            )

        # Step 5: face selection (only when the creator appears in frame)
        creator_in_frame = context.get("creator_in_frame", True)
        face_section = ""
        selected_faces = []
        if creator_in_frame:
            try:
                selected_faces = self.faces.select(tone, content_type)
                face_section = "\n".join(
                    f"  - {ref.path.name} (expression={ref.expression})" for ref in selected_faces
                )
            except FaceLibraryError as exc:
                return TaskResult(
                    agent=self.name, objective=objective, status="needs_input",
                    summary=str(exc),
                    next_action="Add real photographs to face-library/ and re-run.",
                )

        # Steps 6-7: concepts, ranked
        concepts = ConceptGenerator().generate(brief)
        top = concepts[0]

        # Steps 8-9: composition plan + brand integration sheet
        identity = load_brand_identity(brand)
        composition_plan = self._composition_plan(brief, top, identity)
        brand_sheet = self._brand_sheet(brand, identity)

        # Step 11: variant plan
        variants = self._variant_plan(top)
        scale_note = "Check focal point and text legibility when reduced to sidebar size."

        # Step 10: QA on a supplied render, otherwise queued for review
        render_path = context.get("render_path")
        qa_report = None
        if render_path:
            qa_report = VisualQA(brief).run(Path(render_path))

        # Persist the pre-render package (step 12: present for review)
        out_dir = self._output_dir(topic)
        package_path = out_dir / "package.md"
        package_path.write_text(
            self._package_markdown(
                brief, selected_faces, concepts, composition_plan,
                brand_sheet, variants, qa_report, scale_note,
            ),
            encoding="utf-8",
        )
        log("thumbnail_package_written", path=str(package_path), channel=brand.slug)

        summary = (
            f"Channel: {brand.name}. Tone: {tone}. Top concept: {top.headline}. "
            f"Pre-render package written to {package_path}."
        )
        if qa_report:
            summary += f" QA on supplied render: {qa_report.report().splitlines()[-1]}"

        return TaskResult(
            agent=self.name,
            objective=objective,
            status="completed",
            summary=summary,
            artifacts=[str(package_path)],
            next_action=(
                "Review the concepts and confirm one for rendering; supply a render_path to run visual QA."
            ),
        )

    # -------------------------------------------------------------- workflow

    def record_review_feedback(self, feedback: str, scope: str = "thumbnails") -> str:
        """Workflow step 13: record useful feedback via the learning rules."""
        pref = self.memory.record_correction(rule=feedback, scope=scope)
        return (
            f"Recorded as preference '{pref.id}' (confidence={pref.confidence}, "
            f"evidence={pref.evidence_count}). One correction is temporary until repeated."
        )

    # --------------------------------------------------------------- helpers

    @staticmethod
    def _extract_topic(objective: str) -> str:
        """Pull the video topic out of phrases like 'thumbnail for X'."""
        match = re.search(
            r"(?:thumbnail|thumb)\s*(?:for|of|about)?\s*(?:the\s+)?(?:video\s+)?[:\-]?\s*(.+)",
            objective,
            re.IGNORECASE,
        )
        if match:
            topic = match.group(1).strip(" .!?")
            topic = re.sub(r"^(a|an|new)\s+", "", topic, flags=re.IGNORECASE)
            if topic and topic.lower() not in {"video", "the video", "my video"}:
                return topic
        return objective.strip()

    @staticmethod
    def _infer_tone(text: str) -> str:
        lowered = text.lower()
        best, best_hits = "excited", 0
        for tone, keywords in TONE_KEYWORDS.items():
            hits = sum(1 for kw in keywords if kw in lowered)
            if hits > best_hits:
                best, best_hits = tone, hits
        return best

    @staticmethod
    def _slugify(text: str) -> str:
        slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
        return (slug or "thumbnail")[:60]

    def _output_dir(self, topic: str) -> Path:
        from system import paths

        override = self._output_root_override()
        base = override if override is not None else paths.THUMBNAIL_SYSTEM / "output"
        out = base / f"{_dt.date.today().isoformat()}_{self._slugify(topic)}"
        out.mkdir(parents=True, exist_ok=True)
        return out

    def _output_root_override(self) -> Path | None:
        """Test hook: when memory uses a temp root, write packages beside it."""
        if self.memory.root is not None:
            return self.memory.root.parent / "thumbnail-output"
        return None

    @staticmethod
    def _composition_plan(brief: ThumbnailBrief, concept: Concept, identity: dict) -> str:
        """Layer-by-layer render plan for step 8 (generation/editing)."""
        colors = identity.get("colors", {})
        primary = colors.get("Primary", "(brand primary unfilled)")
        lines = [
            "## Composition plan (render-ready)",
            "",
            f"Canvas: 1280x720 minimum, 16:9.",
            f"Pattern: {concept.pattern}",
            "",
            "Layers (back to front):",
            "  1. Background: environment/scene, darkened 15-25% behind text zones",
            "  2. Main event: game element per concept composition",
            "  3. Subject: real face reference (no AI face substitution, preserve identity)",
            "  4. Lighting: key light on subject edge; match scene color temperature",
            "  5. Text: short phrase only, high contrast, away from focal point",
            f"  6. Brand: logo, correct channel only ({brief.channel})",
            "",
            f"Brand primary color reference: {primary}",
            "",
            "Pipeline per AGENT_SPEC: real photograph -> subject extraction -> editing -> "
            "compositing -> environment integration -> QA.",
            "Never generate the creator's face from scratch; composite the real reference.",
        ]
        return "\n".join(lines)

    @staticmethod
    def _brand_sheet(brand: Brand, identity: dict) -> str:
        status = "filled" if identity["filled"] else "UNFILLED — fill brands/%s/VISUAL_IDENTITY.md" % brand.slug
        return (
            f"Brand: {brand.name} ({brand.slug})\n"
            f"Visual identity status: {status}\n"
            "Separation: do not use any other brand's logo, typography, or colors in this thumbnail."
        )

    @staticmethod
    def _variant_plan(top: Concept) -> str:
        return (
            "Planned variants (step 11):\n"
            f"  A. {top.headline} — as specified\n"
            f"  B. Alternate crop: subject right, element left (mirror)\n"
            "  C. Text-free variant (test pure-visual readability)\n"
            "  D. High-contrast variant (stronger color separation)"
        )

    def _package_markdown(
        self,
        brief: ThumbnailBrief,
        faces,
        concepts: list[Concept],
        composition_plan: str,
        brand_sheet: str,
        variants: str,
        qa_report,
        scale_note: str,
    ) -> str:
        parts = [
            f"# Thumbnail package — {brief.video_topic}",
            "",
            brief.to_markdown(),
            "",
            "## Face references selected (step 5)",
            "",
        ]
        if faces:
            parts.extend(f"- {ref.path.name} — expression: {ref.expression}" for ref in faces)
        else:
            parts.append("- (creator not in frame)")
        parts.extend([
            "",
            "## Concepts (steps 6-7, ranked)",
            "",
        ])
        for i, concept in enumerate(concepts, 1):
            parts.append(f"{i}. {concept.describe()}")
        parts.extend([
            "",
            composition_plan,
            "",
            "## Brand integration (step 9)",
            "",
            brand_sheet,
            "",
            "## Visual QA (step 10)",
            "",
        ])
        if qa_report is not None:
            parts.append("```")
            parts.append(qa_report.report())
            parts.append("```")
        else:
            parts.append(
                "No render supplied yet. QA will run when a render path is provided; "
                "perceptual checks require review (QA_RULES.md)."
            )
        parts.extend([
            "",
            "## Review (step 12)",
            "",
            "Awaiting user selection of a concept before rendering. Variants:",
            variants,
            "",
            "Small-scale test: " + scale_note,
        ])
        return "\n".join(parts) + "\n"
