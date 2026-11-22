"""Tests for key persistence (.env + agent_config.yaml), autostart, editing, diagnostics."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from system.llm import client as llm_client
from system.llm.client import LLMError, _parse_simple_yaml, load_config, save_key


def _clean_env():
    """Env snapshot for save/restore around config tests."""
    keys = ("NVIDIA_API_KEY", "NIM_API_KEY", "GROQ_API_KEY", "GEMINI_API_KEY",
            "GOOGLE_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY",
            "SUBHRA_AGENT_PROVIDER")
    return {k: os.environ.pop(k, None) for k in keys}


def _restore_env(snapshot):
    """Restore the snapshot, DELETING keys that were not set before."""
    for key, value in snapshot.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


class TestSaveKeyAndConfig(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="agent-test-config-"))
        self.old_file, self.old_env = llm_client.CONFIG_FILE, _clean_env()
        llm_client.CONFIG_FILE = self.tmp / "agent_config.yaml"

    def tearDown(self):
        llm_client.CONFIG_FILE = self.old_file
        _restore_env(self.old_env)

    def test_save_key_writes_yaml_and_loads(self):
        path = save_key("nvidia", "nvapi-secret-123")
        self.assertTrue(path.exists())
        values = _parse_simple_yaml(path.read_text(encoding="utf-8"))
        self.assertEqual(values["provider"], "nvidia")
        self.assertEqual(values["api_key"], "nvapi-secret-123")
        cfg = load_config()
        self.assertTrue(cfg.enabled)
        self.assertEqual(cfg.provider, "nvidia")
        self.assertEqual(cfg.api_key, "nvapi-secret-123")
        # The exact NVIDIA default rotates as models retire; assert a model resolved.
        self.assertTrue(cfg.model)

    def test_save_key_preserves_other_settings(self):
        llm_client.CONFIG_FILE.write_text("temperature: 0.7\nmax_tokens: 999\n", encoding="utf-8")
        save_key("groq", "gsk_abc")
        values = _parse_simple_yaml(llm_client.CONFIG_FILE.read_text(encoding="utf-8"))
        self.assertEqual(values["temperature"], "0.7")
        self.assertEqual(values["max_tokens"], "999")
        self.assertEqual(values["provider"], "groq")

    def test_save_key_rejects_unknown_provider(self):
        with self.assertRaises(LLMError):
            save_key("skynet", "key")

    def test_key_with_special_chars_round_trips(self):
        save_key("nvidia", 'nvapi-abc"def#ghi: jkl')
        cfg = load_config()
        self.assertEqual(cfg.api_key, 'nvapi-abc"def#ghi: jkl')

    def test_env_key_still_wins_over_file(self):
        save_key("groq", "gsk_file_key")
        with mock.patch.dict(os.environ, {"NVIDIA_API_KEY": "nvapi_env_key"}):
            cfg = load_config()
        self.assertEqual(cfg.provider, "nvidia")
        self.assertEqual(cfg.api_key, "nvapi_env_key")


class TestDotenv(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="agent-test-env-"))
        self.old_file, self.old_dotenv, self.old_env = (
            llm_client.CONFIG_FILE, llm_client.DOTENV_FILE, _clean_env())
        llm_client.CONFIG_FILE = self.tmp / "agent_config.yaml"
        llm_client.DOTENV_FILE = self.tmp / ".env"

    def tearDown(self):
        llm_client.CONFIG_FILE, llm_client.DOTENV_FILE = self.old_file, self.old_dotenv
        _restore_env(self.old_env)

    def test_dotenv_autoloaded(self):
        llm_client.DOTENV_FILE.write_text(
            '# comment\nNVIDIA_API_KEY=nvapi-from-dotenv\nexport GROQ_API_KEY="gsk quoted"\n',
            encoding="utf-8")
        llm_client._load_dotenv()
        cfg = load_config()
        self.assertEqual(cfg.provider, "nvidia")
        self.assertEqual(cfg.api_key, "nvapi-from-dotenv")
        self.assertEqual(os.environ.get("GROQ_API_KEY"), "gsk quoted")

    def test_existing_env_not_overridden(self):
        llm_client.DOTENV_FILE.write_text("NVIDIA_API_KEY=from-file\n", encoding="utf-8")
        with mock.patch.dict(os.environ, {"NVIDIA_API_KEY": "from-env"}):
            llm_client._load_dotenv()
            cfg = load_config()
        self.assertEqual(cfg.api_key, "from-env")


class TestAutostart(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="agent-test-startup-"))
        self.patcher = mock.patch("system.startup.startup_dir", return_value=self.tmp)
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()

    def test_install_and_uninstall(self):
        from system import startup

        path = startup.install_startup()
        self.assertTrue(path.exists())
        body = path.read_text(encoding="utf-8")
        self.assertIn("pythonw", body)
        self.assertIn("cli supervise", body)
        self.assertIn("standalone", body)   # window-hidden launch
        # Regression: the launcher once referenced an undefined `logdir`
        # variable, so cmd failed on the redirect and the server NEVER
        # started — the app window opened to a dead server at every login.
        self.assertNotIn("logdir", body)
        self.assertNotIn("{", body)         # every placeholder substituted
        self.assertIn(startup.LOG_NAME, body)  # logs to a real file path
        self.assertIn(startup.LAUNCHER_LOG_NAME, body)  # crash log, separate file
        self.assertIn("--log ", body)        # server output reaches that file
        self.assertEqual(startup.uninstall_startup(), True)
        self.assertFalse(path.exists())
        self.assertEqual(startup.uninstall_startup(), False)

    def test_status_reflects_install(self):
        from system import startup

        self.assertFalse(startup.startup_status()["installed"])
        startup.install_startup()
        self.assertTrue(startup.startup_status()["installed"])

    def test_install_can_enable_voice_mode(self):
        from system import startup

        path = startup.install_startup(with_voice=True)
        body = path.read_text(encoding="utf-8")
        self.assertIn("--with-voice", body)
        self.assertIn("--voice-log", body)
        self.assertIn(startup.VOICE_LOG_NAME, body)
        self.assertNotIn("{", body)          # every placeholder substituted
        self.assertTrue(startup.startup_status()["voice"])
        startup.install_startup()             # default: no listener child
        self.assertFalse(startup.startup_status()["voice"])

    def test_install_records_share_key_and_tunnel(self):
        from system import startup

        path = startup.install_startup(share=True, pairing_key="abc123", tunnel=True)
        body = path.read_text(encoding="utf-8")
        self.assertIn("--share", body)
        self.assertIn("--key abc123", body)
        self.assertIn("--tunnel", body)
        info = startup.startup_status()
        self.assertTrue(info["shared"])
        self.assertTrue(info["tunnel"])


class TestCliSurvivesConsoleEncoding(unittest.TestCase):
    def test_unprintable_reply_character_does_not_crash_the_cli(self):
        """A non-breaking hyphen (U+2011) in a reply ended a live voice session:
        plain print() raised UnicodeEncodeError on the cp1252 console."""
        import subprocess

        root = Path(__file__).resolve().parent.parent
        code = (
            f"import sys; sys.path.insert(0, r'{root}'); "
            "from cli.__main__ import _make_console_safe; _make_console_safe(); "
            "print('non\\u2011breaking hyphen')"
        )
        env = {**os.environ, "PYTHONIOENCODING": "cp1252"}
        result = subprocess.run([sys.executable, "-c", code], capture_output=True, env=env)
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))


class TestSupervisor(unittest.TestCase):
    """The login launcher runs `cli supervise`: keep exactly one server up."""

    def test_port_open_reflects_listener_state(self):
        import socket

        from system import supervisor

        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            port = listener.getsockname()[1]
            self.assertTrue(supervisor.port_open(port))
        self.assertFalse(supervisor.port_open(port))

    def test_serve_command_flags(self):
        from system import supervisor

        self.assertEqual(supervisor.serve_command("py", 8756),
                         ["py", "-u", "-m", "cli", "serve", "--port", "8756"])
        command = supervisor.serve_command("py", 9000, share=True,
                                           pairing_key="k", tunnel=True)
        self.assertIn("--share", command)
        self.assertIn("--tunnel", command)
        self.assertEqual(command[command.index("--key") + 1], "k")
        # --tunnel alone still implies shared mode (pairing key enforced).
        self.assertIn("--share", supervisor.serve_command("py", 8756, tunnel=True))

    def _stop_after(self, ticks):
        def fake_sleep(seconds):
            ticks[0] += 1
            if ticks[0] > ticks[1]:
                raise KeyboardInterrupt
        return fake_sleep

    def _own_lock(self, supervisor):
        """Pretend this test owns the lock — the real one is held by the
        supervisor running on this machine, which the tests must ignore."""
        return mock.patch.object(supervisor, "acquire_lock",
                                 return_value=mock.MagicMock())

    def test_with_voice_supervises_the_listener_too(self):
        from system import supervisor

        started = []

        class DeadChild:
            pid = 99
            returncode = 1

            def poll(self):
                return 1

        def fake_popen(cmd, **kwargs):
            started.append(cmd)
            return DeadChild()

        with self._own_lock(supervisor), \
             mock.patch.object(supervisor.subprocess, "Popen", fake_popen), \
             mock.patch.object(supervisor, "port_open", return_value=False), \
             mock.patch.object(supervisor.time, "sleep", self._stop_after([0, 9])):
            with self.assertRaises(KeyboardInterrupt):
                supervisor.supervise(port=8756, with_voice=True)
        self.assertTrue(any("serve" in cmd for cmd in started))    # server child
        self.assertTrue(any("listen" in cmd for cmd in started))   # voice child

    def test_lock_allows_only_one_supervisor(self):
        from system import supervisor

        tmp = Path(tempfile.mkdtemp(prefix="agent-test-lock-"))
        lock_path = tmp / "supervisor.lock"
        first = supervisor.acquire_lock(lock_path)
        self.assertIsNotNone(first)
        self.assertIsNone(supervisor.acquire_lock(lock_path))   # second refused
        first.close()
        second = supervisor.acquire_lock(lock_path)
        self.assertIsNotNone(second)      # released on close (and on crash)
        second.close()

    def test_supervise_exits_when_another_supervisor_runs(self):
        from system import supervisor

        with mock.patch.object(supervisor, "acquire_lock", return_value=None):
            with mock.patch.object(supervisor.subprocess, "Popen",
                                   side_effect=AssertionError("must not spawn")):
                self.assertEqual(supervisor.supervise(port=8756), 0)

    def test_supervise_relaunches_a_server_that_died(self):
        from system import supervisor

        starts = []

        class DeadChild:
            pid = 4321
            returncode = 1

            def poll(self):
                return self.returncode

        clock = {"t": 1000.0}

        def fake_monotonic():
            clock["t"] += 3.0      # real time advances while the dead child is noticed
            return clock["t"]

        with self._own_lock(supervisor), \
             mock.patch.object(supervisor.subprocess, "Popen",
                               side_effect=lambda cmd, **kw: starts.append(cmd) or DeadChild()), \
             mock.patch.object(supervisor, "port_open", return_value=False), \
             mock.patch.object(supervisor.time, "monotonic", fake_monotonic), \
             mock.patch.object(supervisor.time, "sleep", self._stop_after([0, 10])):
            with self.assertRaises(KeyboardInterrupt):
                supervisor.supervise(port=8756)
        self.assertGreaterEqual(len(starts), 2)     # relaunched after it died

    def test_a_child_that_keeps_dying_backs_off(self):
        from system import supervisor

        class Dead:
            pid = 1
            returncode = 3

            def poll(self):
                return self.returncode

        child = supervisor._Child("voice", ["python", "-m", "cli", "listen"])
        delays, now = [], 0.0
        with mock.patch.object(supervisor.subprocess, "Popen", return_value=Dead()):
            for _ in range(4):
                supervisor._tick(child, now, "127.0.0.1")            # spawn (dies at once)
                supervisor._tick(child, now + 0.1, "127.0.0.1")      # notice the death
                delays.append(child.delay)
                now += child.delay + 0.1
        self.assertEqual(delays, [10.0, 20.0, 40.0, 60.0])           # capped backoff

        healthy = supervisor._Child("server", ["x"])
        healthy.process = Dead()
        healthy.started_at = 10_000.0
        supervisor._tick(healthy, 10_000.0 + supervisor.HEALTHY_SECONDS + 1, "127.0.0.1")
        self.assertEqual(healthy.delay, supervisor.RESTART_DELAY_SECONDS)  # reset

    def test_supervise_sends_child_output_to_the_log(self):
        from system import supervisor

        tmp = Path(tempfile.mkdtemp(prefix="agent-test-supervise-"))
        logfile = tmp / "serve-autostart.log"
        seen = {}

        class Child:
            pid = 7
            returncode = 1

            def poll(self):
                return 1

        def fake_popen(cmd, **kwargs):
            seen.update(kwargs)
            return Child()

        with self._own_lock(supervisor), \
             mock.patch.object(supervisor.subprocess, "Popen", fake_popen), \
             mock.patch.object(supervisor, "port_open", return_value=False), \
             mock.patch.object(supervisor.time, "sleep", self._stop_after([0, 3])):
            with self.assertRaises(KeyboardInterrupt):
                supervisor.supervise(port=8756, log_path=logfile)
        handle = seen.get("stdout")
        self.assertIsNotNone(handle)                      # explicit, not inherited
        handle.close()
        text = logfile.read_text(encoding="utf-8")
        self.assertIn("SURA supervisor started", text)
        self.assertIn("listening on :8756", text)

    def test_supervise_never_starts_a_second_server(self):
        from system import supervisor

        with self._own_lock(supervisor), \
             mock.patch.object(supervisor, "port_open", return_value=True), \
             mock.patch.object(supervisor.subprocess, "Popen",
                               side_effect=AssertionError("must not spawn")), \
             mock.patch.object(supervisor.time, "sleep", self._stop_after([0, 3])):
            with self.assertRaises(KeyboardInterrupt):
                supervisor.supervise(port=8756)


class TestVoiceMirroring(unittest.TestCase):
    """`cli listen` must hand each spoken turn to the chat screen."""

    def test_listen_mirrors_both_sides_of_the_exchange(self):
        import argparse

        from cli import __main__ as cli_main
        from system import server as server_module
        from system.voice import assistant as assistant_module

        captured = {}

        class FakeAssistant:
            def __init__(self, events=None, **kwargs):
                captured["events"] = events

            def listen(self) -> None:
                captured["events"].on_transcript("open notepad")
                captured["events"].on_reply("Launched Notepad.")

        posted = []
        with mock.patch.object(assistant_module, "VoiceAssistant", FakeAssistant), \
             mock.patch.object(server_module, "post_voice_turn",
                               lambda role, text, **kwargs: posted.append((role, text))):
            exit_code = cli_main._cmd_listen(argparse.Namespace(download_model=False))
        self.assertEqual(exit_code, 0)
        self.assertEqual(posted, [("you", "open notepad"), ("sura", "Launched Notepad.")])


class TestAppWaitsForServer(unittest.TestCase):
    def test_app_mirrors_spoken_turns_into_the_chat(self):
        app = (Path(__file__).resolve().parent.parent / "ui" / "app.js").read_text(
            encoding="utf-8")
        self.assertIn("/api/voice/transcript", app)
        self.assertIn("pollVoiceTurns()", app)

    def test_app_boot_waits_for_the_server(self):
        app = (Path(__file__).resolve().parent.parent / "ui" / "app.js").read_text(
            encoding="utf-8")
        self.assertIn("async function waitForServer()", app)
        self.assertIn("await waitForServer();", app)


class TestEditingAgent(unittest.TestCase):
    def _stylebook(self, tmp: Path):
        from system.agents.editing.editing_agent import StyleBook

        return StyleBook(path=tmp / "styles.json")

    def test_learn_parses_numbers_and_links(self):
        tmp = Path(tempfile.mkdtemp(prefix="agent-test-editing-"))
        book = self._stylebook(tmp)
        out = book.learn(
            "MrBeast hook under 5 sec, avg shot 2 sec, cut fast on movement. "
            "zoom punch in 120% on reactions. use speed ramp on replays. "
            "sound: whoosh on every transition https://youtu.be/dQw4w9WgXcQ"
        )
        self.assertEqual(out["creator"], "mrbeast")
        profile = book.get("mrbeast")
        self.assertEqual(profile["hook_seconds"], 5.0)
        self.assertEqual(profile["avg_shot_seconds"], 2.0)
        self.assertIn("https://youtu.be/dQw4w9WgXcQ", profile["sources"])
        self.assertTrue(any("ramp" in e for e in profile["effects"]))
        self.assertTrue(any("whoosh" in s for s in profile["sound_design"]))

    def test_learn_merges_into_same_profile(self):
        tmp = Path(tempfile.mkdtemp(prefix="agent-test-editing-"))
        book = self._stylebook(tmp)
        book.learn("mrbeast avg shot 2 sec")
        book.learn("mrbeast hook 4 sec more notes")
        self.assertEqual(len(book.names()), 1)
        profile = book.get("mrbeast")
        self.assertEqual(profile["hook_seconds"], 4.0)
        self.assertEqual(profile["avg_shot_seconds"], 2.0)

    def test_merged_overrides_baseline(self):
        tmp = Path(tempfile.mkdtemp(prefix="agent-test-editing-"))
        book = self._stylebook(tmp)
        book.learn("mrbeast avg shot 1.5 sec")
        merged = book.merged()
        self.assertEqual(merged["avg_shot_seconds"], 1.5)
        self.assertEqual(merged["hook_seconds"], 8)  # baseline kept

    def test_plan_written_to_disk(self):
        tmp = Path(tempfile.mkdtemp(prefix="agent-test-editing-"))
        from system.agents.editing import editing_agent as mod

        with mock.patch.object(mod, "EDITING_PLANS_DIR", tmp / "plans"):
            path, meta = mod.build_edit_plan("Valorant ranked grind ep 4",
                                             stylebook=self._stylebook(tmp))
        text = path.read_text(encoding="utf-8")
        self.assertIn("Edit plan", text)
        self.assertIn("Valorant", text)
        self.assertIn("Hook", text)
        self.assertIn("Export checklist", text)

    def test_agent_handle_plan_mode(self):
        tmp = Path(tempfile.mkdtemp(prefix="agent-test-editing-"))
        from system.agents.editing import editing_agent as mod
        from system.memory import MemoryStore

        memory = MemoryStore(root=tmp / "mem")
        with mock.patch.object(mod, "EDITING_PLANS_DIR", tmp / "plans"):
            agent = mod.EditingAgent(memory=memory, stylebook=self._stylebook(tmp))
            result = agent.handle("edit plan for gta v drift video", {"topic": "gta v drift"})
        self.assertEqual(result.status, "completed")
        self.assertTrue(any(Path(a).exists() for a in result.artifacts))

    def test_plan_read_name_validation(self):
        from system.agents.editing.editing_agent import read_plan

        self.assertIn("error", read_plan("../evil"))
        self.assertIn("error", read_plan("missing-plan"))


class TestVoiceDiagnostics(unittest.TestCase):
    def test_dep_check_reports_missing_gracefully(self):
        from system.voice import diagnostics

        results = diagnostics.check_deps()
        names = {r["name"] for r in results}
        self.assertIn("dep:sounddevice", names)
        for section in results:
            self.assertIn(section["status"], ("ok", "fail"))
            self.assertTrue(section["name"])

    def test_voice_check_never_crashes(self):
        from system.voice import diagnostics

        with mock.patch.object(diagnostics, "check_mic",
                               return_value={"name": "microphone", "status": "fail",
                                             "detail": "skip", "hints": []}):
            sections = diagnostics.voice_check()
        self.assertTrue(any(s["name"] == "stt_model" for s in sections))
        self.assertTrue(any(s["name"] == "voiceprint" for s in sections))
        self.assertTrue(all("detail" in s for s in sections))


class TestLLMUrlJoin(unittest.TestCase):
    """Regression: base URLs written with /v1 must not produce /v1/v1/..."""

    def test_openai_style_base_with_version(self):
        url = llm_client.LLMClient._url("https://integrate.api.nvidia.com/v1",
                                        "v1/chat/completions")
        self.assertEqual(url, "https://integrate.api.nvidia.com/v1/chat/completions")

    def test_openai_style_base_without_version(self):
        url = llm_client.LLMClient._url("https://api.openai.com",
                                        "v1/chat/completions")
        self.assertEqual(url, "https://api.openai.com/v1/chat/completions")

    def test_anthropic_and_models_endpoints(self):
        base = "https://api.anthropic.com"
        self.assertEqual(llm_client.LLMClient._url(base, "v1/messages"),
                         f"{base}/v1/messages")
        self.assertEqual(llm_client.LLMClient._url(base + "/v1", "v1/models"),
                         f"{base}/v1/models")

    def test_ollama_native_path_not_mangled(self):
        base = "http://localhost:11434"
        self.assertEqual(llm_client.LLMClient._url(base, "api/chat"),
                         f"{base}/api/chat")


class _FakeSSE:
    """A urlopen() stand-in that streams OpenAI-style SSE chunks."""

    def __init__(self, lines):
        self._lines = lines

    def __iter__(self):
        return iter(self._lines)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class TestLLMStreaming(unittest.TestCase):
    """Token streaming: SSE parsing, tool-fragment assembly, interruption."""

    @staticmethod
    def _client(provider="nvidia"):
        cfg = llm_client.LLMConfig(provider=provider, api_key="k", model="m",
                                   base_url="https://example.test/v1")
        return llm_client.LLMClient(cfg)

    def test_sse_chunks_become_text_events(self):
        from unittest import mock

        sse = _FakeSSE([
            b'data: {"choices":[{"delta":{"content":"Hel"}}]}\n\n',
            b'data: {"choices":[{"delta":{"content":"lo"}}]}\n\n',
            b"data: [DONE]\n\n",
        ])
        with mock.patch("urllib.request.urlopen", return_value=sse):
            events = list(self._client().chat_stream([{"role": "user", "content": "hi"}]))
        text = "".join(e["text"] for e in events if e["type"] == "text")
        self.assertEqual(text, "Hello")
        self.assertEqual(events[-1]["type"], "done")

    def test_sse_tool_fragments_accumulate(self):
        from unittest import mock

        sse = _FakeSSE([
            b'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"c1",'
            b'"function":{"name":"remember","arguments":"{\\"q\\":"}}]}}]}\n\n',
            b'data: {"choices":[{"delta":{"tool_calls":[{"index":0,'
            b'"function":{"arguments":"\\"x\\"}"}}]}}]}\n\n',
            b"data: [DONE]\n\n",
        ])
        with mock.patch("urllib.request.urlopen", return_value=sse):
            events = list(self._client().chat_stream([{"role": "user", "content": "hi"}]))
        calls = [e for e in events if e["type"] == "tool_calls"]
        self.assertEqual(len(calls), 1)
        call = calls[0]["tool_calls"][0]
        self.assertEqual(call["id"], "c1")
        self.assertEqual(call["name"], "remember")
        self.assertEqual(call["arguments"], {"q": "x"})

    def test_stream_interrupted_carries_partial_text(self):
        from unittest import mock

        class _Dies(_FakeSSE):
            def __iter__(self):
                yield b'data: {"choices":[{"delta":{"content":"par"}}]}\n\n'
                raise urllib.error.URLError("connection reset")

        with mock.patch("urllib.request.urlopen", return_value=_Dies([])):
            with self.assertRaises(llm_client.StreamInterrupted) as ctx:
                list(self._client().chat_stream([{"role": "user", "content": "hi"}]))
        self.assertEqual(ctx.exception.partial_text, "par")

    def test_non_streaming_provider_falls_back_to_one_event(self):
        client = self._client("anthropic")
        whole = llm_client.LLMResponse(text="whole reply")
        with mock.patch.object(client, "_chat_anthropic", return_value=whole):
            events = list(client.chat_stream([{"role": "user", "content": "hi"}]))
        self.assertEqual([e["type"] for e in events], ["text", "done"])
        self.assertEqual(events[0]["text"], "whole reply")

    def test_agent_loop_stream_falls_back_between_providers(self):
        from types import SimpleNamespace

        from system.llm.agent_loop import AgentLoop

        class _Dead:
            config = SimpleNamespace(provider="dead")

            def chat_stream(self, messages, tools=None):
                raise llm_client.LLMError("quota gone")
                yield  # pragma: no cover — makes this a generator

        class _Alive:
            config = SimpleNamespace(provider="alive")

            def chat_stream(self, messages, tools=None):
                yield {"type": "text", "text": "rescued"}
                yield {"type": "done"}

        from system.memory import MemoryStore

        loop = object.__new__(AgentLoop)
        loop.clients = [_Dead(), _Alive()]
        loop.memory = MemoryStore(root=Path(tempfile.mkdtemp(prefix="agent-test-loop-")))
        loop.max_turns = 2
        loop.tools = []
        events = list(loop.run_stream("hi"))
        self.assertEqual("".join(e["text"] for e in events if e["type"] == "text"),
                         "rescued")
        self.assertEqual(events[-1]["reply"], "rescued")


class TestWhisperTranscriberDedupe(unittest.TestCase):
    """The whisper transcriber must never emit the same prefix twice.

    Uses fakes so no audio hardware, model, or network is needed.
    """

    class _FakeWhisperModel:
        def __init__(self, scripted: str) -> None:
            self.scripted = scripted
            self.calls = 0

        def transcribe(self, wav_bytes, **_kwargs):
            self.calls += 1
            segment = type("_Seg", (), {"text": self.scripted})()
            return [segment], None

    def _transcriber_with(self, scripted):
        from system.voice.stt_tts import Transcriber

        t = object.__new__(Transcriber)
        t._vosk = None
        t._model = self._FakeWhisperModel(scripted)
        t._prefix = ""
        t._buffer = b""
        t._last_sound = 0.0
        t._speech_seen = False
        t._started = False
        return t

    def test_overlap_not_duplicated(self):
        t = self._transcriber_with("hey sura run status")
        first = t._emit("hey sura")
        self.assertEqual(first, "hey sura")
        # Next window re-decodes the same audio: 'hey sura run status'.
        text = t._decode_window()
        self.assertEqual(text, "run status")

    def test_silent_feed_never_decodes(self):
        t = self._transcriber_with("nothing")
        silence = b"\x00\x00" * 800          # 0.1 s of digital silence
        for _ in range(20):
            self.assertIsNone(t.feed(silence))
        self.assertEqual(t._model.calls, 0)  # VAD: no decode without speech

    def test_engine_name_reports_whisper_when_installed(self):
        from system.voice.stt_tts import stt_engine_name

        self.assertIn(stt_engine_name(), ("whisper", "vosk"))


class TestPcTools(unittest.TestCase):
    """Phone → laptop control: shell commands, app launch, push-to-phone."""

    def _dispatch(self, name, args):
        from system.llm.tools import build_tools, dispatch
        from system.memory import MemoryStore

        tools = build_tools(
            MemoryStore(root=Path(tempfile.mkdtemp(prefix="agent-test-pctools-")))
        )
        return dispatch(tools, name, args)

    def test_run_pc_command_captures_output(self):
        out = self._dispatch("run_pc_command", {"command": "echo sura_ok"})
        self.assertIn("exit=0", out)
        self.assertIn("sura_ok", out)

    def test_run_pc_command_blocks_destructive(self):
        out = self._dispatch("run_pc_command", {"command": "format c: /quiet"})
        self.assertTrue(out.startswith("DENIED"))

    def test_run_pc_command_empty(self):
        out = self._dispatch("run_pc_command", {"command": "   "})
        self.assertTrue(out.startswith("ERROR"))

    def test_open_app_requires_name(self):
        out = self._dispatch("open_app", {"app": "  "})
        self.assertTrue(out.startswith("ERROR"))

    def test_push_to_phone_roundtrip(self):
        from system import server as server_mod

        inbox = Path(tempfile.mkdtemp(prefix="agent-test-push2-")) / "push_inbox.jsonl"
        with mock.patch.object(server_mod, "PUSH_FILE", inbox):
            out = self._dispatch("push_to_phone", {"text": "hello from sura"})
        self.assertIn("pushed", out)
        self.assertIn("hello from sura", inbox.read_text(encoding="utf-8"))


class TestVoiceThresholdConfig(unittest.TestCase):
    """voice_threshold in agent_config.yaml overrides the 0.90 default."""

    def test_config_knob_changes_threshold(self):
        from system.voice.wake import SpeakerVerifier, _configured_threshold

        with mock.patch(
            "system.llm.client._parse_simple_yaml",
            return_value={"voice_threshold": "0.72"},
        ):
            self.assertAlmostEqual(_configured_threshold(), 0.72)
            self.assertAlmostEqual(SpeakerVerifier().threshold, 0.72)

    def test_bad_value_falls_back_to_default(self):
        from system.voice.wake import SpeakerVerifier, _configured_threshold

        with mock.patch(
            "system.llm.client._parse_simple_yaml",
            return_value={"voice_threshold": "banana"},
        ):
            self.assertAlmostEqual(_configured_threshold(), 0.90)
        with mock.patch(
            "system.llm.client._parse_simple_yaml",
            return_value={"voice_threshold": "9.5"},
        ):
            self.assertAlmostEqual(_configured_threshold(), 0.99)  # clamped

    def test_explicit_arg_still_wins(self):
        from system.voice.wake import SpeakerVerifier

        self.assertAlmostEqual(SpeakerVerifier(threshold=0.5).threshold, 0.5)


if __name__ == "__main__":
    unittest.main()
