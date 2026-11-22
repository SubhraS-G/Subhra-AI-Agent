"""Visual QA.

Implements QA_RULES.md as structured checks. Technical checks are executed
against real files; perceptual checks (identity, natural skin, clutter) are
produced as an explicit review checklist, because honest verification
requires either image analysis or human review — the agent must not claim
perceptual passes it cannot observe (BEHAVIOR_RULES rule 5).
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path

from system.agents.creative.brief import ThumbnailBrief
from system.verification import Verification

TARGET_ASPECT = 16 / 9
TARGET_WIDTH = 1280
TARGET_HEIGHT = 720


def _png_size(path: Path) -> tuple[int, int] | None:
    try:
        data = path.read_bytes()[:33]
        if data[:8] != b"\x89PNG\r\n\x1a\n" or data[12:16] != b"IHDR":
            return None
        width, height = struct.unpack(">II", data[16:24])
        return width, height
    except OSError:
        return None


def _jpeg_size(path: Path) -> tuple[int, int] | None:
    try:
        data = path.read_bytes()
    except OSError:
        return None
    if data[:2] != b"\xff\xd8":
        return None
    i = 2
    while i + 9 < len(data):
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        seg_len = struct.unpack(">H", data[i + 2 : i + 4])[0]
        if marker in (0xC0, 0xC1, 0xC2, 0xC3):
            h, w = struct.unpack(">HH", data[i + 5 : i + 9])
            return w, h
        i += 2 + seg_len
    return None


def image_size(path: Path) -> tuple[int, int] | None:
    """Return (width, height) for PNG/JPEG, else None."""
    return _png_size(path) or _jpeg_size(path)


@dataclass
class VisualQA:
    """Runs technical checks and builds the perceptual review checklist."""

    brief: ThumbnailBrief

    def run(self, image_path: Path, verified_face: bool = False) -> Verification:
        v = Verification(subject=f"thumbnail {image_path.name}")
        problems: list[str] = []

        # ---- Technical (QA_RULES: Technical) --------------------------------
        if not image_path.exists():
            v.add("file exists", False, f"missing: {image_path}")
            return v  # cannot continue meaningfully
        v.add("file exists", True)

        size = image_size(image_path)
        if size is None:
            v.add("valid file", False, "unrecognized image format (PNG/JPEG supported)")
            return v
        v.add("valid file", True, f"{size[0]}x{size[1]}")

        width, height = size
        aspect = width / height
        aspect_ok = abs(aspect - TARGET_ASPECT) < 0.02
        v.add(
            "correct aspect ratio (16:9)",
            aspect_ok,
            f"actual {aspect:.3f}",
        )
        v.add(
            "correct resolution (>= 1280x720)",
            width >= TARGET_WIDTH and height >= TARGET_HEIGHT,
            f"actual {width}x{height}",
        )

        # ---- Perceptual (QA_RULES: Face / Objects / Composition / Branding) --
        # The agent records these as an explicit checklist; marking them
        # verified requires observation it does not currently have.
        perceptual = [
            "Face: recognizable identity",
            "Face: expression matches brief tone",
            "Face: natural skin, no facial artifacts",
            "Objects: correct game assets, no warped objects",
            "Objects: no duplicated elements or malformed hands",
            "Composition: clear focal point survives small-scale test",
            "Composition: good contrast, no clutter",
            "Branding: correct channel identity (no brand mixing)",
        ]
        for item in perceptual:
            v.add(item, None, "requires review")

        # Verification.passed only reflects executable checks; the report
        # separates them clearly.
        return v


def small_scale_summary(image_path: Path) -> str:
    """Report what a human should check when the thumbnail is reduced."""
    size = image_size(image_path)
    if not size:
        return "Cannot evaluate: not a readable image."
    width, height = size
    small_w = 168  # approx thumbnail size in YouTube subscriptions sidebar
    small_h = round(small_w * height / width)
    return (
        f"Small-scale test target: {small_w}x{small_h}px. "
        "Confirm the focal point and text are still legible at that size."
    )
