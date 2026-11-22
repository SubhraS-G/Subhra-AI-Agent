"""Tests for the core agent system.

Run:  python -m pytest tests/ -q   (or python -m unittest discover tests)
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def hermetic_memory():
    """A MemoryStore over a temp dir so tests never touch real memory/."""
    from system.memory import MemoryStore

    return MemoryStore(root=Path(tempfile.mkdtemp(prefix="agent-test-memory-")))

from system.agents.creative.brief import ThumbnailBrief
from system.agents.creative.face_library import FaceLibrary, FaceLibraryError
from system.brands import BrandError, BrandRegistry
from system.memory import MemoryStore
from system.orchestrator import Orchestrator
from system.permissions import PermissionDenied, PermissionGate, Tier


class TestMemoryLearning(unittest.TestCase):
    def test_single_correction_is_low_confidence(self):
        store = hermetic_memory()
        pref = store.record_correction(rule="test rule minimal text", scope="test")
        self.assertEqual(pref.confidence, "low")
        self.assertEqual(pref.evidence_count, 1)

    def test_repeated_corrections_promote(self):
        store = hermetic_memory()
        store.record_correction(rule="test repeated rule", scope="test")
        pref = store.record_correction(rule="test repeated rule", scope="test")
        self.assertEqual(pref.evidence_count, 2)
        self.assertGreaterEqual(
            ["unknown", "low", "medium", "high", "established"].index(pref.confidence), 1
        )


class TestPermissions(unittest.TestCase):
    def test_automatic_action_allowed(self):
        gate = PermissionGate(granted=Tier.AUTOMATIC)
        self.assertTrue(gate.check("research"))

    def test_publish_blocked_at_automatic(self):
        gate = PermissionGate(granted=Tier.AUTOMATIC)
        with self.assertRaises(PermissionDenied):
            gate.require("publish_content")

    def test_unknown_actions_fail_closed(self):
        gate = PermissionGate(granted=Tier.AUTOMATIC)
        self.assertFalse(gate.check("totally_unknown_action"))


class TestBrands(unittest.TestCase):
    def test_resolve_subhra_gaming(self):
        brand = BrandRegistry().resolve("thumbnail for Valorant highlights on Subhra Gaming")
        self.assertEqual(brand.slug, "subhra-gaming")

    def test_paused_brand_in_text_rejected(self):
        registry = BrandRegistry()
        with self.assertRaises(BrandError):
            registry.assert_separation("Bring back GTA V Labs")

    def test_unknown_brand_rejected(self):
        with self.assertRaises(BrandError):
            BrandRegistry().resolve("some unrelated channel")


class TestBrief(unittest.TestCase):
    def test_long_text_overlay_flagged(self):
        brief = ThumbnailBrief(
            video_topic="topic",
            channel="subhra-gaming",
            text_overlay="THIS IS A WAY TOO LONG SENTENCE THAT DUPLICATES THE FULL VIDEO TITLE",
        )
        self.assertTrue(any("text_overlay" in p for p in brief.validate()))


class TestFaceLibrary(unittest.TestCase):
    def test_empty_library_raises_for_creator_tasks(self):
        # The real library may have photos; use an isolated empty dir.
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            lib = FaceLibrary(root=Path(tmp))
            with self.assertRaises(FaceLibraryError):
                lib.select("excited")


class TestOrchestrator(unittest.TestCase):
    def test_thumbnail_request_produces_package(self):
        orchestrator = Orchestrator(memory=hermetic_memory())
        result = orchestrator.request(
            "thumbnail for a Valorant highlight",
            {"channel_override": "subhra-gaming", "creator_in_frame": False},
        )
        self.assertEqual(result.status, "completed")
        self.assertTrue(result.artifacts)
        for artifact in result.artifacts:
            self.assertTrue(Path(artifact).exists(), artifact)
            self.assertIn("thumbnail-output", str(artifact))

    def test_missing_channel_asks_instead_of_guessing(self):
        orchestrator = Orchestrator(memory=hermetic_memory())
        result = orchestrator.request("make a thumbnail about something vague")
        self.assertEqual(result.status, "needs_input")


if __name__ == "__main__":
    unittest.main()
