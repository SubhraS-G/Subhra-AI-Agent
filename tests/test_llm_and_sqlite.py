"""Tests for the LLM brain and SQLite memory.

The LLM tests use a FakeClient so no network or API key is ever needed.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from system.llm.client import LLMClient, LLMConfig, LLMError, LLMResponse, load_config
from system.llm.system_prompt import build_system_prompt
from system.memory import MemoryStore
from system.sqlite_memory import SQLiteMemoryStore


def hermetic_memory():
    return MemoryStore(root=Path(tempfile.mkdtemp(prefix="agent-test-memory-")))


def sqlite_memory():
    return SQLiteMemoryStore(db_path=Path(tempfile.mkdtemp(prefix="agent-test-db-")) / "memory.db")


class TestLLMConfig(unittest.TestCase):
    def test_no_config_is_rule_mode(self):
        import os

        from system.llm import client as client_mod

        old = {k: os.environ.pop(k, None) for k in
               ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "SUBHRA_AGENT_PROVIDER",
                "NVIDIA_API_KEY", "NIM_API_KEY", "GROQ_API_KEY",
                "GEMINI_API_KEY", "GOOGLE_API_KEY")}
        # Isolate the config file too: a real key saved on this machine
        # (config/agent_config.yaml) must not leak into the hermetic test.
        from unittest import mock

        with mock.patch.object(client_mod, "CONFIG_FILE",
                               Path(tempfile.mkdtemp()) / "none.yaml"):
            try:
                cfg = load_config()
                self.assertFalse(cfg.enabled)
            finally:
                for key, value in old.items():
                    if value is not None:
                        os.environ[key] = value

    def test_nvidia_key_auto_detected(self):
        import os
        from unittest import mock

        env = {k: "" for k in ("SUBHRA_AGENT_PROVIDER", "GROQ_API_KEY",
                               "GEMINI_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY")}
        with mock.patch.dict(os.environ, {**env, "NVIDIA_API_KEY": "nvapi-test"}, clear=False):
            cfg = load_config()
        self.assertEqual(cfg.provider, "nvidia")
        self.assertEqual(cfg.api_key, "nvapi-test")
        self.assertEqual(cfg.base_url, "https://integrate.api.nvidia.com/v1")
        # The exact default rotates as NVIDIA retires models; any non-empty
        # default model must be resolved.
        self.assertTrue(cfg.model)

    def test_provider_priority_nvidia_first(self):
        import os
        from unittest import mock

        with mock.patch.dict(os.environ, {
            "NVIDIA_API_KEY": "nvapi-a", "GROQ_API_KEY": "gsk-b",
            "SUBHRA_AGENT_PROVIDER": "",
        }, clear=False):
            cfg = load_config()
        self.assertEqual(cfg.provider, "nvidia")

    def test_explicit_provider_override(self):
        import os
        from unittest import mock

        with mock.patch.dict(os.environ, {
            "SUBHRA_AGENT_PROVIDER": "groq", "GROQ_API_KEY": "gsk-x",
            "NVIDIA_API_KEY": "nvapi-y",
        }, clear=False):
            cfg = load_config()
        self.assertEqual(cfg.provider, "groq")
        self.assertEqual(cfg.api_key, "gsk-x")

    def test_get_clients_builds_fallback_chain(self):
        import os
        from unittest import mock
        from system.llm.client import get_clients

        with mock.patch.dict(os.environ, {
            "SUBHRA_AGENT_PROVIDER": "nvidia", "NVIDIA_API_KEY": "nvapi-a",
            "GROQ_API_KEY": "gsk-b", "GEMINI_API_KEY": "gm-c",
        }, clear=False):
            clients = get_clients()
        providers = [c.config.provider for c in clients]
        self.assertEqual(providers[0], "nvidia")
        self.assertIn("groq", providers)
        self.assertIn("google", providers)

    def test_client_requires_provider(self):
        with self.assertRaises(LLMError):
            LLMClient(LLMConfig())

    def test_unknown_provider_rejected(self):
        with self.assertRaises(LLMError):
            LLMClient(LLMConfig(provider="skynet", api_key="x"))


class TestMessageNormalization(unittest.TestCase):
    def test_anthropic_tool_roundtrip(self):
        messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "",
             "tool_calls": [{"id": "t1", "name": "agent_status", "arguments": {}}]},
            {"role": "tool", "tool_call_id": "t1", "name": "agent_status", "content": "{}"},
        ]
        out = LLMClient._normalize_anthropic(messages)
        self.assertEqual(out[0]["role"], "system")
        self.assertEqual(out[-1]["content"][0]["type"], "tool_result")
        self.assertEqual(out[-1]["content"][0]["tool_use_id"], "t1")
        # Empty assistant text produces tool_use-only content (no empty text block).
        assistant = out[2]
        self.assertEqual(len(assistant["content"]), 1)
        self.assertEqual(assistant["content"][0]["type"], "tool_use")
        self.assertEqual(assistant["content"][0]["name"], "agent_status")

    def test_openai_tool_roundtrip(self):
        messages = [
            {"role": "assistant", "content": None,
             "tool_calls": [{"id": "t1", "name": "search_memory", "arguments": {"query": "x"}}]},
            {"role": "tool", "tool_call_id": "t1", "name": "search_memory", "content": "r"},
        ]
        out = LLMClient._normalize_openai(messages)
        self.assertEqual(out[0]["tool_calls"][0]["function"]["arguments"], '{"query": "x"}')
        self.assertEqual(out[1]["tool_call_id"], "t1")


class _FakeClient:
    """Duck-typed stand-in for LLMClient: calls one tool, then answers."""

    def __init__(self) -> None:
        self.calls = 0

    def chat(self, messages, tools=None):
        self.calls += 1
        if self.calls == 1:
            return LLMResponse(
                text="",
                tool_calls=[{"id": "f1", "name": "agent_status", "arguments": {}}],
            )
        tool_results = [m for m in messages if m.get("role") == "tool"]
        return LLMResponse(text=f"status checked: {len(tool_results)} result(s)", tool_calls=[])


class TestAgentLoop(unittest.TestCase):
    def test_loop_executes_tool_and_answers(self):
        from system.llm.agent_loop import AgentLoop

        memory = sqlite_memory()
        loop = AgentLoop(_FakeClient(), memory, max_turns=4)
        result = loop.run("what is your status?")
        self.assertIn("status checked", result["reply"])
        self.assertEqual(result["tool_trace"][0]["tool"], "agent_status")

    def test_loop_falls_back_to_second_provider(self):
        from system.llm.agent_loop import AgentLoop

        class DyingClient:
            config = LLMConfig(provider="nvidia")

            def chat(self, messages, tools=None):
                raise LLMError("HTTP 429: rate limit exceeded")

        memory = sqlite_memory()
        loop = AgentLoop(DyingClient(), memory, max_turns=4,
                         fallback_clients=[_FakeClient()])
        result = loop.run("status please")
        self.assertIn("status checked", result["reply"])

    def test_loop_reports_when_all_providers_fail(self):
        from system.llm.agent_loop import AgentLoop

        class DeadClient:
            config = LLMConfig(provider="groq")

            def chat(self, messages, tools=None):
                raise LLMError("HTTP 429")

        memory = sqlite_memory()
        loop = AgentLoop(DeadClient(), memory, max_turns=2,
                         fallback_clients=[DeadClient()])
        result = loop.run("anything")
        self.assertIn("All configured LLM providers failed", result["reply"])

    def test_loop_returns_tool_results_to_model(self):
        from system.llm.agent_loop import AgentLoop

        memory = sqlite_memory()
        seen = {}

        class InspectClient(_FakeClient):
            def chat(self, messages, tools=None):
                self.calls += 1
                if self.calls == 1:
                    return LLMResponse(text="", tool_calls=[
                        {"id": "f2", "name": "write_memory",
                         "arguments": {"memory_class": "projects", "name": "demo", "content": "hello"}}
                    ])
                seen["tool_messages"] = [m for m in messages if m.get("role") == "tool"]
                return LLMResponse(text="saved", tool_calls=[])

        loop = AgentLoop(InspectClient(), memory, max_turns=4)
        result = loop.run("remember hello")
        self.assertEqual(result["reply"], "saved")
        tool_messages = seen["tool_messages"]
        self.assertEqual(len(tool_messages), 1)
        self.assertEqual(tool_messages[0]["name"], "write_memory")
        self.assertIn("Stored as", tool_messages[0]["content"])
        self.assertEqual(memory.read_note("projects", "demo"), "hello\n")


class TestSQLiteMemory(unittest.TestCase):
    def test_write_read_search(self):
        store = sqlite_memory()
        store.write_note("projects", "valorant", "Valorant ranked grind video ideas")
        self.assertIn("ranked grind", store.read_note("projects", "valorant"))
        hits = store.search("ranked")
        self.assertTrue(any(h["name"] == "valorant" for h in hits))

    def test_search_finds_preferences(self):
        store = sqlite_memory()
        store.record_correction("prefer bold yellow text", scope="thumbnails")
        hits = store.search("yellow")
        self.assertTrue(
            any("prefer_bold_yellow_text" in h["name"] for h in hits),
            f"preference not searchable: {hits}",
        )

    def test_correction_progression_matches_file_store(self):
        store = sqlite_memory()
        pref = store.record_correction("minimal thumbnail text", scope="test")
        self.assertEqual((pref.confidence, pref.evidence_count), ("low", 1))
        pref = store.record_correction("minimal thumbnail text", scope="test")
        self.assertEqual(pref.evidence_count, 2)
        history = store.correction_history(pref.id)
        self.assertEqual(len(history), 2)

    def test_links(self):
        store = sqlite_memory()
        store.write_note("projects", "valorant", "a")
        store.write_note("workflows", "thumbnails", "b")
        self.assertTrue(store.link_notes(("projects", "valorant"), ("workflows", "thumbnails")))
        links = store.links_for("projects", "valorant")
        self.assertIn(("workflows", "thumbnails"), [(l["memory_class"], l["name"]) for l in links])

    def test_import_from_file_store(self):
        file_store = hermetic_memory()
        file_store.write_note("projects", "imported", "note body")
        file_store.record_correction("imported rule", scope="test")
        file_store.record_correction("imported rule", scope="test")
        store = SQLiteMemoryStore.from_file_store(
            file_store, db_path=Path(tempfile.mkdtemp(prefix="agent-test-db-")) / "m.db"
        )
        self.assertEqual(store.read_note("projects", "imported"), "note body\n")
        prefs = store.list_preferences()
        self.assertEqual(prefs[0].evidence_count, 2)


class TestSystemPrompt(unittest.TestCase):
    def test_prompt_includes_rules_and_preferences(self):
        memory = sqlite_memory()
        memory.record_correction("avoid red overlays", scope="thumbnails")
        prompt = build_system_prompt(memory)
        self.assertIn("SURA", prompt)
        self.assertIn("Unified Reasoning Assistant", prompt)
        self.assertIn("Behavior rules", prompt)
        self.assertIn("avoid red overlays", prompt)
        self.assertIn("never bypass", prompt.lower())

    def test_prompt_flags_unfilled_profile(self):
        """When USER_PROFILE.md is still a template, the prompt says so."""
        from unittest import mock

        from system import core_docs

        template_docs = core_docs.CoreDocs(
            agent_spec="# spec\n## 1. Vision\ntest",
            behavior_rules="# rules\n## Core Behavior\nbe direct",
            permissions="# permissions\n## Default Policy\nautomatic",
            user_profile="# USER PROFILE\n> Fill this file with information",
            memory_doc="",
        )
        with mock.patch.object(core_docs, "load_core_docs", return_value=template_docs):
            prompt = build_system_prompt(hermetic_memory())
        self.assertIn("not filled yet", prompt)


if __name__ == "__main__":
    unittest.main()
