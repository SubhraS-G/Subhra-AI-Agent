"""Tests for the local UI server (hermetic — real Orchestrator, temp memory)."""

from __future__ import annotations

import sys
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from system.memory import MemoryStore
from system.orchestrator import Orchestrator
from system.server import AgentRequestHandler, ThreadingHTTPServer


def _start_server() -> tuple[ThreadingHTTPServer, int]:
    memory = MemoryStore(root=Path(tempfile.mkdtemp(prefix="agent-test-server-")))
    orchestrator = Orchestrator(memory=memory)
    handler = type("BoundHandler", (AgentRequestHandler,), {"orchestrator": orchestrator})
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd, httpd.server_address[1]


class TestServer(unittest.TestCase):
    httpd = None
    port = None

    @classmethod
    def setUpClass(cls):
        cls.httpd, cls.port = _start_server()
        cls.base = f"http://127.0.0.1:{cls.port}"

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()

    def _get(self, path: str) -> tuple[int, str]:
        with urllib.request.urlopen(f"{self.base}{path}", timeout=10) as response:
            return response.status, response.read().decode("utf-8")

    def _post(self, path: str, payload: dict) -> tuple[int, str]:
        request = urllib.request.Request(
            f"{self.base}{path}",
            data=__import__("json").dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, response.read().decode("utf-8")

    def test_index_served(self):
        status, body = self._get("/")
        self.assertEqual(status, 200)
        self.assertIn("SURA", body)

    def test_status_endpoint(self):
        status, body = self._get("/api/status")
        self.assertEqual(status, 200)
        self.assertIn('"agent"', body)
        self.assertIn("face_library", body)

    def test_preferences_endpoint_lists_valid_entries(self):
        import json as jsonlib

        status, body = self._get("/api/preferences")
        self.assertEqual(status, 200)
        data = jsonlib.loads(body)
        self.assertIsInstance(data["preferences"], list)
        for pref in data["preferences"]:
            self.assertIn("rule", pref)
            self.assertIn("confidence", pref)

    def test_learn_roundtrip(self):
        status, body = self._post("/api/learn", {"rule": "server test rule", "scope": "test"})
        self.assertEqual(status, 200)
        self.assertIn("confidence=low", body)
        status, body = self._get("/api/preferences")
        self.assertIn("server test rule", body)

    def test_chat_rule_mode(self):
        status, body = self._post("/api/chat", {"message": "hello"})
        self.assertEqual(status, 200)
        self.assertIn("reply", body)

    def test_chat_requires_message(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._post("/api/chat", {"message": ""})
        self.assertEqual(ctx.exception.code, 400)

    def test_search_endpoint(self):
        status, body = self._get("/api/search?q=anything")
        self.assertEqual(status, 200)

    def test_packages_endpoint(self):
        status, body = self._get("/api/packages")
        self.assertEqual(status, 200)
        self.assertIn("packages", body)


class TestSharedModeAuth(unittest.TestCase):
    """Phone-access (LAN share) mode.

    Two servers: a loopback-trusted one (what the PC itself hits) and a
    remote-rules one (trust_loopback=False — exactly what a phone's request
    is subject to, since tests can only originate from 127.0.0.1).
    """

    httpd = None
    port = None
    remote_httpd = None
    remote_port = None
    KEY = "testkey42"

    @staticmethod
    def _make_handler(trust_loopback: bool):
        memory = MemoryStore(root=Path(tempfile.mkdtemp(prefix="agent-test-share-")))
        orchestrator = Orchestrator(memory=memory)
        return type("BoundHandler", (AgentRequestHandler,), {
            "orchestrator": orchestrator,
            "pairing_key": TestSharedModeAuth.KEY,
            "trust_loopback": trust_loopback,
        })

    @classmethod
    def setUpClass(cls):
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), cls._make_handler(True))
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.port = cls.httpd.server_address[1]
        cls.base = f"http://127.0.0.1:{cls.port}"
        cls.remote_httpd = ThreadingHTTPServer(("127.0.0.1", 0), cls._make_handler(False))
        threading.Thread(target=cls.remote_httpd.serve_forever, daemon=True).start()
        cls.remote_port = cls.remote_httpd.server_address[1]
        cls.remote_base = f"http://127.0.0.1:{cls.remote_port}"

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.remote_httpd.shutdown()

    def _get(self, path, headers=None, remote=False):
        base = self.remote_base if remote else self.base
        request = urllib.request.Request(f"{base}{path}", headers=headers or {})
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, response.read().decode("utf-8")

    def test_pairing_helpers(self):
        from system.server import generate_pairing_key, sanitize_key

        key = generate_pairing_key()
        self.assertEqual(len(key), 8)
        self.assertEqual(sanitize_key(key), key)
        self.assertEqual(sanitize_key("AbC-1!x"), "abc1x")   # junk stripped

    def test_loopback_bypasses_key(self):
        status, body = self._get("/api/status")
        self.assertEqual(status, 200)
        self.assertIn("face_library", body)

    def test_remote_without_key_rejected(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._get("/api/status", remote=True)
        self.assertEqual(ctx.exception.code, 401)

    def test_remote_wrong_key_rejected(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._get("/api/status", {"X-SURA-Key": "wrong-key"}, remote=True)
        self.assertEqual(ctx.exception.code, 401)

    def test_remote_correct_key_accepted(self):
        status, body = self._get("/api/status", {"X-SURA-Key": self.KEY.upper()}, remote=True)
        self.assertEqual(status, 200)
        self.assertIn("face_library", body)

    def test_pair_endpoint_reports_match(self):
        import json as jsonlib

        _, yes = self._get(f"/api/pair?key={self.KEY}", remote=True)
        self.assertTrue(jsonlib.loads(yes)["ok"])
        _, no = self._get("/api/pair?key=nope", remote=True)
        self.assertFalse(jsonlib.loads(no)["ok"])

    def test_status_reports_shared(self):
        _, body = self._get("/api/status")
        import json as jsonlib

        self.assertTrue(jsonlib.loads(body)["shared"])


class TestStreamingAndPush(unittest.TestCase):
    """NDJSON chat streaming, the push inbox, and the tailscale endpoint.

    The streaming orchestrator is a stub (a real LLM would be a live wire);
    everything else runs against the real server, handler and pairing gate.
    """

    @classmethod
    def setUpClass(cls):
        import json as jsonlib
        from system.agents.base import TaskResult

        class StreamOrchestrator:
            """Mimics Orchestrator.chat_stream without touching the network."""

            memory = MemoryStore(root=Path(tempfile.mkdtemp(prefix="agent-test-stream-")))

            def chat_stream(self, message, history=None):
                yield {"type": "text", "text": "Hel"}
                yield {"type": "text", "text": "lo, "}
                yield {"type": "text", "text": "world"}
                yield {"type": "done", "reply": "Hello, world", "tool_trace": [], "mode": "llm"}

            def chat(self, message, history=None):
                return {"reply": "Hello, world", "tool_trace": [], "mode": "llm"}

            def record_preference(self, rule, scope="general"):
                return self.memory.record_correction(rule=rule, scope=scope)

        cls.jsonlib = jsonlib
        memory = MemoryStore(root=Path(tempfile.mkdtemp(prefix="agent-test-push-")))
        handler = type("BoundHandler", (AgentRequestHandler,), {
            "orchestrator": StreamOrchestrator(),
        })
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.port = cls.httpd.server_address[1]
        cls.base = f"http://127.0.0.1:{cls.port}"
        del TaskResult

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()

    def _post(self, path: str, payload: dict):
        request = urllib.request.Request(
            f"{self.base}{path}",
            data=self.jsonlib.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        return urllib.request.urlopen(request, timeout=10)

    def test_chat_stream_yields_ndjson_events(self):
        with self._post("/api/chat/stream", {"message": "hi"}) as response:
            self.assertEqual(response.status, 200)
            self.assertIn("ndjson", response.headers.get("Content-Type", ""))
            lines = [line for line in response.read().decode("utf-8").splitlines() if line]
        events = [self.jsonlib.loads(line) for line in lines]
        kinds = [e["type"] for e in events]
        self.assertEqual(kinds[0], "text")
        self.assertEqual(kinds[-1], "done")
        self.assertEqual("".join(e["text"] for e in events if e["type"] == "text"),
                         "Hello, world")
        self.assertEqual(events[-1]["reply"], "Hello, world")

    def test_chat_stream_requires_message(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._post("/api/chat/stream", {"message": ""}).read()
        self.assertEqual(ctx.exception.code, 400)

    def test_push_roundtrip(self):
        with self._post("/api/push", {"text": "link from the laptop"}) as response:
            body = self.jsonlib.loads(response.read().decode("utf-8"))
        self.assertTrue(body["id"])
        self.assertEqual(body["text"], "link from the laptop")
        status = urllib.request.urlopen(
            f"{self.base}/api/push/poll?since=0", timeout=10)
        data = self.jsonlib.loads(status.read().decode("utf-8"))
        texts = [item["text"] for item in data["items"]]
        self.assertIn("link from the laptop", texts)

    def test_push_poll_since_filters(self):
        with self._post("/api/push", {"text": "older note"}) as response:
            item = self.jsonlib.loads(response.read().decode("utf-8"))
        status = urllib.request.urlopen(
            f"{self.base}/api/push/poll?since={item['at']}", timeout=10)
        data = self.jsonlib.loads(status.read().decode("utf-8"))
        self.assertNotIn("older note", [i["text"] for i in data["items"]])

    def test_push_requires_text(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._post("/api/push", {"text": ""}).read()
        self.assertEqual(ctx.exception.code, 400)

    def test_tailscale_endpoint_reports_state(self):
        status = urllib.request.urlopen(f"{self.base}/api/tailscale", timeout=10)
        data = self.jsonlib.loads(status.read().decode("utf-8"))
        self.assertIn("installed", data)
        self.assertIn("running", data)

    def test_push_inbox_helpers(self):
        from system.server import push_message, read_push_inbox

        item = push_message("helper test", source="test")
        items = read_push_inbox(since_epoch=item["at"] - 1)
        self.assertTrue(any(i["id"] == item["id"] for i in items))
        self.assertEqual(read_push_inbox(since_epoch=item["at"] + 9999), [])


class TestVoiceTranscript(unittest.TestCase):
    """Spoken turns must be readable on the chat screen (and continue-able)."""

    httpd = None
    port = None

    @classmethod
    def setUpClass(cls):
        cls.httpd, cls.port = _start_server()
        cls.base = f"http://127.0.0.1:{cls.port}"

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()

    def test_voice_turns_reach_the_transcript(self):
        import json
        from unittest import mock

        from system import server as srv

        tmp = Path(tempfile.mkdtemp(prefix="agent-test-voice-turns-")) / "voice.jsonl"
        with mock.patch.object(srv, "VOICE_TURN_FILE", tmp):
            for role, text in (("you", "hey sura open notepad"),
                               ("sura", "Launched Notepad.")):
                request = urllib.request.Request(
                    f"{self.base}/api/voice/turn",
                    data=json.dumps({"role": role, "text": text}).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(request, timeout=10) as response:
                    self.assertEqual(response.status, 200)

            def fetch(query: str) -> dict:
                url = f"{self.base}/api/voice/transcript{query}"
                return json.loads(urllib.request.urlopen(url, timeout=10).read())

            turns = [(i["role"], i["text"]) for i in fetch("?tail=10")["items"]]
            self.assertIn(("you", "hey sura open notepad"), turns)
            self.assertIn(("sura", "Launched Notepad."), turns)
            # Incremental polling must not repeat what the UI already showed.
            newest = max(i["at"] for i in fetch("?tail=10")["items"])
            self.assertEqual(fetch(f"?since={newest}")["items"], [])

    def test_voice_turn_requires_text(self):
        import json

        request = urllib.request.Request(
            f"{self.base}/api/voice/turn",
            data=json.dumps({"role": "you", "text": "  "}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(request, timeout=10)
        self.assertEqual(ctx.exception.code, 400)


class TestQuietDisconnects(unittest.TestCase):
    """A client leaving mid-response must not dump a traceback in the log."""

    def test_client_disconnect_is_quiet_real_errors_are_not(self):
        from unittest import mock

        from system import server as srv

        httpd = srv.QuietHTTPServer.__new__(srv.QuietHTTPServer)   # no socket needed
        with mock.patch.object(srv.ThreadingHTTPServer, "handle_error") as parent:
            try:
                raise ConnectionAbortedError(10053, "aborted by the host")
            except ConnectionAbortedError:
                httpd.handle_error(None, ("127.0.0.1", 55555))
            parent.assert_not_called()          # quiet: no traceback
            try:
                raise ValueError("a real bug")
            except ValueError:
                httpd.handle_error(None, ("127.0.0.1", 55555))
            parent.assert_called_once()         # real errors still surface


if __name__ == "__main__":
    unittest.main()
