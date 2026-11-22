"""LLM configuration and client.

ROADMAP Tier 2a: the reasoning brain. Provider-agnostic, free-first:

- NVIDIA NIM (OpenAI-compatible) — NVIDIA_API_KEY  (~1,000 free credits, no card)
- Groq (OpenAI-compatible)       — GROQ_API_KEY    (free tier, rate-limited)
- Google Gemini (OpenAI-compat)  — GEMINI_API_KEY  (free tier via AI Studio)
- Ollama (local model)           — SUBHRA_AGENT_PROVIDER=ollama (unlimited, private)
- Anthropic / OpenAI             — paid, also supported

Configuration resolution order:
1. environment variables (first key found wins, NVIDIA prioritized)
2. config/.env            (auto-loaded if present; gitignored)
3. config/agent_config.yaml (gitignored)
4. no provider -> system runs in rule-based mode

Windows tip: `set NVIDIA_API_KEY=...` only lasts for that one terminal window.
SURA persists keys to config/agent_config.yaml instead (python -m cli set-key),
so every new terminal, the web UI, and voice mode all see the key.

Only the standard library is used here; provider SDKs are never required.
"""

from __future__ import annotations

import json
import os
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from system import paths
from system.logging_utils import log

CONFIG_FILE = paths.AGENT_ROOT / "config" / "agent_config.yaml"
DOTENV_FILE = paths.AGENT_ROOT / "config" / ".env"

DEFAULT_MODELS = {
    # meta/llama-3.3-70b-instruct reached end-of-life on NVIDIA NIM (2026-08);
    # nemotron-3-super is NVIDIA's current flagship chat model with tools.
    "nvidia": "nvidia/nemotron-3-super-120b-a12b",
    "groq": "openai/gpt-oss-120b",
    "google": "gemini-2.5-flash",
    "anthropic": "claude-sonnet-4-20250514",
    "openai": "gpt-4o-mini",
    "ollama": "llama3.1",
}

DEFAULT_BASE_URLS = {
    "nvidia": "https://integrate.api.nvidia.com/v1",
    "groq": "https://api.groq.com/openai/v1",
    "google": "https://generativelanguage.googleapis.com/v1beta/openai",
    "anthropic": "https://api.anthropic.com",
    "openai": "https://api.openai.com",
    "ollama": "http://localhost:11434",
}

# OpenAI-compatible providers reuse the OpenAI chat path.
OPENAI_COMPATIBLE = {"nvidia", "groq", "google", "openai"}

# Cloudflare-fronted APIs (NVIDIA NIM) reject the default Python-urllib UA.
USER_AGENT = "SURA/1.0"

# Auto-detection order: env-var names per provider. NVIDIA first — Subhra's key.
ENV_PROVIDERS = [
    ("nvidia", ("NVIDIA_API_KEY", "NIM_API_KEY")),
    ("groq", ("GROQ_API_KEY",)),
    ("google", ("GEMINI_API_KEY", "GOOGLE_API_KEY")),
    ("anthropic", ("ANTHROPIC_API_KEY",)),
    ("openai", ("OPENAI_API_KEY",)),
]


def _load_dotenv() -> None:
    """Load KEY=VALUE pairs from config/.env into os.environ (once).

    Existing environment variables always win — the file only fills blanks.
    Lines are `KEY=VALUE`, `# comments` allowed, optional `export ` prefix,
    optional surrounding quotes. No third-party dotenv needed.
    """
    if not DOTENV_FILE.exists():
        return
    try:
        for raw in DOTENV_FILE.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            if line.lower().startswith("export "):
                line = line[len("export "):]
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'").strip()
            if key and key not in os.environ:
                os.environ[key] = value
    except OSError as exc:
        log("dotenv_load_failed", error=str(exc))


_load_dotenv()


def _quote(value: str) -> str:
    """Quote a YAML scalar so keys with spaces/special chars round-trip."""
    if not value:
        return '""'
    if any(ch in value for ch in ":#\"'") or value != value.strip():
        return '"' + value.replace('\\', '\\\\').replace('"', '\\"') + '"'
    return value


def save_key(provider: str, api_key: str, model: str = "") -> Path:
    """Persist an API key (and optional model) into config/agent_config.yaml.

    Returns the config path. The file is gitignored. Existing settings are
    preserved; `provider`, `api_key` and `model` (when given) are updated.
    """
    provider = provider.strip().lower()
    if provider not in DEFAULT_MODELS:
        raise LLMError(f"Unknown provider '{provider}'. Use one of: {', '.join(DEFAULT_MODELS)}")
    values: dict[str, str] = {}
    if CONFIG_FILE.exists():
        values = _parse_simple_yaml(CONFIG_FILE.read_text(encoding="utf-8"))
    values["provider"] = provider
    values["api_key"] = api_key.strip()
    if model:
        values["model"] = model.strip()
    elif values.get("model") and provider not in values.get("model", ""):
        # A model tuned for another provider is a footgun; reset to default.
        values.pop("model", None)
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(
        "\n".join(f"{key}: {_quote(str(val))}" for key, val in values.items()) + "\n",
        encoding="utf-8",
    )
    log("llm_key_saved", provider=provider)
    return CONFIG_FILE


class LLMError(Exception):
    pass


class StreamInterrupted(LLMError):
    """A streamed reply died mid-flight; `.partial_text` holds what arrived."""

    def __init__(self, partial_text: str, detail: str) -> None:
        super().__init__(detail)
        self.partial_text = partial_text


@dataclass
class LLMConfig:
    provider: str = ""          # "" -> no LLM (rule-based mode)
    model: str = ""
    api_key: str = ""
    base_url: str = ""
    max_tokens: int = 2048
    temperature: float = 0.4

    @property
    def enabled(self) -> bool:
        return bool(self.provider)


def load_config() -> LLMConfig:
    """Resolve LLM config from environment, config/.env, then agent_config.yaml."""
    cfg = LLMConfig()

    # 1) Explicit provider choice via SUBHRA_AGENT_PROVIDER.
    provider = os.environ.get("SUBHRA_AGENT_PROVIDER", "").strip().lower()
    if provider:
        cfg.provider = provider
        cfg.api_key = _find_key(provider)
    else:
        # Auto-detect: first provider with a key present (NVIDIA first).
        # Env vars include keys loaded from config/.env by _load_dotenv().
        for name, env_vars in ENV_PROVIDERS:
            for env_var in env_vars:
                if os.environ.get(env_var, "").strip():
                    cfg.provider = name
                    cfg.api_key = os.environ[env_var].strip()
                    break
            if cfg.provider:
                break

    # 2) Config file fills whatever the environment left blank.
    if CONFIG_FILE.exists():
        file_vals = _parse_simple_yaml(CONFIG_FILE.read_text(encoding="utf-8"))
        if not cfg.provider:
            cfg.provider = str(file_vals.get("provider", "")).strip().lower()
            if cfg.provider and not cfg.api_key:
                cfg.api_key = str(file_vals.get("api_key", "")).strip()
        cfg.model = str(file_vals.get("model", "")).strip()
        if file_vals.get("base_url"):
            cfg.base_url = str(file_vals["base_url"]).strip()
        if file_vals.get("max_tokens"):
            try:
                cfg.max_tokens = int(file_vals["max_tokens"])
            except ValueError:
                pass
        if file_vals.get("temperature"):
            try:
                cfg.temperature = float(file_vals["temperature"])
            except ValueError:
                pass

    # 3) Apply provider defaults; ollama needs no key.
    if cfg.provider and cfg.provider not in DEFAULT_MODELS:
        raise LLMError(
            f"Unknown provider '{cfg.provider}'. Use one of: {', '.join(DEFAULT_MODELS)}"
        )
    if cfg.provider and not cfg.model:
        cfg.model = DEFAULT_MODELS[cfg.provider]
    if cfg.provider and not cfg.base_url:
        cfg.base_url = DEFAULT_BASE_URLS[cfg.provider]
    if cfg.provider and not cfg.api_key and cfg.provider != "ollama":
        raise LLMError(
            f"provider '{cfg.provider}' set but no API key found "
            f"(expected env: {', '.join(_env_names_for(cfg.provider))})"
        )

    return cfg


def _env_names_for(provider: str) -> tuple[str, ...]:
    for name, env_vars in ENV_PROVIDERS:
        if name == provider:
            return env_vars
    return ()


def _find_key(provider: str) -> str:
    """Find an API key for a provider from its env names.

    OpenAI-compatible providers also accept OPENAI_API_KEY — that is the
    documented pattern for tools that 'pretend to be OpenAI'.
    """
    for env_var in _env_names_for(provider):
        value = os.environ.get(env_var, "").strip()
        if value:
            return value
    if provider in OPENAI_COMPATIBLE:
        return os.environ.get("OPENAI_API_KEY", "").strip()
    return ""


def _unescape(text: str) -> str:
    """Left-to-right unescape for the two escapes _quote writes (\\" and \\\\)."""
    out, i = [], 0
    while i < len(text):
        if text[i] == "\\" and i + 1 < len(text) and text[i + 1] in ('"', "\\"):
            out.append(text[i + 1])
            i += 2
        else:
            out.append(text[i])
            i += 1
    return "".join(out)


def _parse_simple_yaml(text: str) -> dict:
    """Tiny flat-YAML parser (key: value lines) — no dependencies needed.

    Comments (`# ...`) are stripped only outside quotes, so quoted values
    like api_key: "nvapi-abc#def" survive intact.
    """
    values: dict = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, _, value = line.partition(":")
        value = value.strip()
        if value[:1] in ('"', "'"):
            quote = value[0]
            i, end = 1, -1
            while i < len(value):                    # find closing quote, honoring \" escapes
                if value[i] == "\\" and i + 1 < len(value):
                    i += 2
                    continue
                if value[i] == quote:
                    end = i
                    break
                i += 1
            content = value[1:end] if end != -1 else value[1:]
            values[key.strip()] = _unescape(content)
            continue
        line = value.split("#", 1)[0].strip()   # unquoted: strip comment
        if line:
            values[key.strip()] = line
    return values


@dataclass
class ToolSpec:
    """A tool the LLM may call, in provider-neutral form."""

    name: str
    description: str
    parameters: dict  # JSON schema
    handler: callable


@dataclass
class LLMResponse:
    text: str
    tool_calls: list = field(default_factory=list)
    stop_reason: str = ""
    raw: dict = field(default_factory=dict)


class LLMClient:
    """Minimal provider-agnostic chat client with tool calling.

    Uses plain HTTPS requests so no SDK install is required.
    """

    def __init__(self, config: LLMConfig | None = None) -> None:
        self.config = config or load_config()
        if not self.config.enabled:
            raise LLMError("No LLM provider configured (rule-based mode active).")
        if self.config.provider not in DEFAULT_MODELS:
            raise LLMError(
                f"Unknown provider '{self.config.provider}'. "
                f"Use one of: {', '.join(DEFAULT_MODELS)}"
            )

    # ------------------------------------------------------------------ chat

    def chat(self, messages: list[dict], tools: list[ToolSpec] | None = None) -> LLMResponse:
        """One provider-neutral chat turn.

        messages: [{"role": "user"|"assistant"|"system"|"tool", ...}, ...]
        Tool conversations use the neutral shape:
          assistant: {"role": "assistant", "content": str, "tool_calls": [{id, name, arguments}]}
          tool:      {"role": "tool", "tool_call_id": str, "name": str, "content": str}
        and are translated per provider here.
        """
        if self.config.provider == "anthropic":
            return self._chat_anthropic(self._normalize_anthropic(messages), tools)
        if self.config.provider in OPENAI_COMPATIBLE:
            return self._chat_openai(self._normalize_openai(messages), tools)
        if self.config.provider == "ollama":
            return self._chat_ollama(self._normalize_openai(messages), tools)
        raise LLMError(f"Unsupported provider '{self.config.provider}'")

    def chat_stream(self, messages: list[dict], tools: list[ToolSpec] | None = None):
        """Stream one provider-neutral chat turn as structured events.

        The single biggest perceived-latency fix: the reply appears token by
        token instead of after the whole generation. Yields, in order:

            {"type": "text", "text": chunk}        (many of these)
            {"type": "tool_calls", "tool_calls": [...]}   (only when the model calls tools)
            {"type": "done"}

        Providers without an SSE path fall back to a single whole-reply text
        event, so callers need only one code path. Raises LLMError before any
        text was produced (callers may try another provider), or
        StreamInterrupted (carrying `.partial_text`) when a stream died
        mid-reply.
        """
        if self.config.provider in OPENAI_COMPATIBLE:
            yield from self._chat_openai_stream(self._normalize_openai(messages), tools)
            return
        # No SSE path for this provider: one whole-reply event keeps the
        # caller's code identical.
        response = self.chat(messages, tools)
        if response.text:
            yield {"type": "text", "text": response.text}
        if response.tool_calls:
            yield {"type": "tool_calls", "tool_calls": response.tool_calls}
        yield {"type": "done"}

    def _chat_openai_stream(self, messages: list[dict], tools: list[ToolSpec] | None):
        """SSE streaming for OpenAI-compatible providers (nvidia/groq/google/openai).

        Parses `data: {...}` lines, yielding content deltas as they arrive and
        accumulating tool-call fragments until the stream closes.
        """
        body: dict = {
            "model": self.config.model,
            "messages": messages,
            "temperature": self.config.temperature,
            "stream": True,
        }
        if tools:
            body["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.parameters,
                    },
                }
                for t in tools
            ]
        request = urllib.request.Request(
            self._url(self.config.base_url, "v1/chat/completions"),
            data=json.dumps(body).encode("utf-8"),
            headers={
                "User-Agent": USER_AGENT,          # Cloudflare rejects the default UA
                "Authorization": f"Bearer {self.config.api_key}",
                "Content-Type": "application/json",
                "Accept": "text/event-stream",
            },
            method="POST",
        )
        text_parts: list[str] = []
        tool_acc: dict[int, dict] = {}             # index -> {id, name, arguments}
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                for raw_line in response:
                    line = raw_line.decode("utf-8", errors="replace").strip()
                    if not line.startswith("data:"):
                        continue
                    payload = line[len("data:"):].strip()
                    if payload == "[DONE]":
                        break
                    try:
                        event = json.loads(payload)
                    except json.JSONDecodeError:
                        continue
                    choice = (event.get("choices") or [{}])[0]
                    delta = choice.get("delta") or {}
                    content = delta.get("content")
                    if content:
                        text_parts.append(content)
                        yield {"type": "text", "text": content}
                    for frag in delta.get("tool_calls") or []:
                        index = frag.get("index", 0)
                        acc = tool_acc.setdefault(
                            index, {"id": "", "name": "", "arguments": ""}
                        )
                        if frag.get("id"):
                            acc["id"] = frag["id"]
                        fn = frag.get("function") or {}
                        if fn.get("name"):
                            acc["name"] += fn["name"]    # some providers chunk names
                        if fn.get("arguments"):
                            acc["arguments"] += fn["arguments"]
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
            log("llm_http_error", status=exc.code, detail=detail)
            if not text_parts:
                raise LLMError(f"LLM HTTP {exc.code}: {detail}") from exc
            raise StreamInterrupted("".join(text_parts), f"LLM HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            log("llm_unreachable", url=self.config.base_url)
            if not text_parts:
                raise LLMError(f"LLM endpoint unreachable: {exc.reason}") from exc
            raise StreamInterrupted("".join(text_parts), f"connection lost: {exc.reason}") from exc
        calls = []
        for i, acc in sorted(tool_acc.items()):
            try:
                arguments = json.loads(acc["arguments"] or "{}")
            except json.JSONDecodeError:
                arguments = acc["arguments"]          # agent loop degrades gracefully
            calls.append({"id": acc["id"] or f"call_{i}", "name": acc["name"], "arguments": arguments})
        if calls:
            yield {"type": "tool_calls", "tool_calls": calls}
        yield {"type": "done"}

    def list_models(self) -> list[str]:
        """List model IDs from an OpenAI-compatible provider (nvidia/groq/openai/google)."""
        if self.config.provider not in OPENAI_COMPATIBLE:
            raise LLMError(f"model listing not supported for provider '{self.config.provider}'")
        request = urllib.request.Request(
            self._url(self.config.base_url, "v1/models"),
            headers={"Authorization": f"Bearer {self.config.api_key}",
                     "User-Agent": USER_AGENT},
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                data = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise LLMError(f"model listing HTTP {exc.code}") from exc
        except urllib.error.URLError as exc:
            raise LLMError(f"model listing unreachable: {exc.reason}") from exc
        return [m.get("id", "") for m in data.get("data", []) if m.get("id")]

    # ---------------------------------------------------- message translation

    @staticmethod
    def _normalize_anthropic(messages: list[dict]) -> list[dict]:
        """Neutral history -> Anthropic shape (tool_result lives in user turns)."""
        out: list[dict] = []
        for msg in messages:
            role = msg.get("role")
            if role == "system":
                out.append(msg)
            elif role == "tool":
                block = {
                    "type": "tool_result",
                    "tool_use_id": msg.get("tool_call_id", ""),
                    "content": msg.get("content", ""),
                }
                if out and out[-1].get("role") == "user" and isinstance(out[-1].get("content"), list):
                    out[-1]["content"].append(block)
                else:
                    out.append({"role": "user", "content": [block]})
            elif role == "assistant" and msg.get("tool_calls"):
                content: list[dict] = []
                if msg.get("content"):
                    content.append({"type": "text", "text": msg["content"]})
                for call in msg["tool_calls"]:
                    content.append({
                        "type": "tool_use",
                        "id": call.get("id", call.get("name", "")),
                        "name": call.get("name", ""),
                        "input": call.get("arguments", {}),
                    })
                out.append({"role": "assistant", "content": content})
            else:
                out.append({"role": role, "content": msg.get("content", "")})
        return out

    @staticmethod
    def _normalize_openai(messages: list[dict]) -> list[dict]:
        """Neutral history -> OpenAI/Ollama shape."""
        out: list[dict] = []
        for msg in messages:
            if msg.get("role") == "assistant" and msg.get("tool_calls"):
                out.append({
                    "role": "assistant",
                    "content": msg.get("content") or None,
                    "tool_calls": [
                        {
                            "id": call.get("id", call.get("name", "")),
                            "type": "function",
                            "function": {
                                "name": call.get("name", ""),
                                "arguments": json.dumps(call.get("arguments", {})),
                            },
                        }
                        for call in msg["tool_calls"]
                    ],
                })
            else:
                cleaned = {"role": msg.get("role"), "content": msg.get("content", "")}
                if msg.get("role") == "tool":
                    cleaned["tool_call_id"] = msg.get("tool_call_id", "")
                    cleaned["name"] = msg.get("name", "")
                out.append(cleaned)
        return out

    # ------------------------------------------------------------- providers

    @staticmethod
    def _url(base_url: str, suffix: str) -> str:
        """Join base_url and suffix without doubling the version segment.

        base_url may be written with the version (".../v1", recommended) or
        without it ("https://api.openai.com"). The path suffix carries its
        own version segment ("/v1/chat/completions"), so both spellings end
        up at the same endpoint. Without this join, ".../v1" + "/v1/chat/..."
        produced the 404 that made every NVIDIA request dead.
        """
        base = base_url.rstrip("/")
        suffix = suffix.lstrip("/")
        if base.endswith("/v1") and suffix.startswith("v1/"):
            suffix = suffix[len("v1/"):]
        elif base.endswith("/v1beta") and suffix.startswith("v1beta/"):
            suffix = suffix[len("v1beta/"):]
        elif base.endswith("/v1alpha") and suffix.startswith("v1alpha/"):
            suffix = suffix[len("v1alpha/"):]
        return f"{base}/{suffix}"

    def _chat_anthropic(self, messages: list[dict], tools: list[ToolSpec] | None) -> LLMResponse:
        system_text = "\n\n".join(
            m["content"] for m in messages if m["role"] == "system"
        )
        rest = [m for m in messages if m["role"] != "system"]
        body: dict = {
            "model": self.config.model,
            "max_tokens": self.config.max_tokens,
            "temperature": self.config.temperature,
            "messages": rest,
        }
        if system_text:
            body["system"] = system_text
        if tools:
            body["tools"] = [
                {
                    "name": t.name,
                    "description": t.description,
                    "input_schema": t.parameters,
                }
                for t in tools
            ]
        data = self._post(
            self._url(self.config.base_url, "v1/messages"),
            headers={
                "x-api-key": self.config.api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            payload=body,
        )
        text_parts = [b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"]
        calls = [
            {
                "id": b.get("id", ""),
                "name": b.get("name", ""),
                "arguments": b.get("input", {}),
            }
            for b in data.get("content", [])
            if b.get("type") == "tool_use"
        ]
        return LLMResponse(text="".join(text_parts), tool_calls=calls,
                           stop_reason=data.get("stop_reason", ""), raw=data)

    def _chat_openai(self, messages: list[dict], tools: list[ToolSpec] | None) -> LLMResponse:
        body: dict = {
            "model": self.config.model,
            "messages": messages,
            "temperature": self.config.temperature,
        }
        if tools:
            body["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.parameters,
                    },
                }
                for t in tools
            ]
        data = self._post(
            self._url(self.config.base_url, "v1/chat/completions"),
            headers={
                "Authorization": f"Bearer {self.config.api_key}",
                "Content-Type": "application/json",
            },
            payload=body,
        )
        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message", {})
        calls = [
            {
                "id": c.get("id", ""),
                "name": c.get("function", {}).get("name", ""),
                "arguments": json.loads(c.get("function", {}).get("arguments") or "{}"),
            }
            for c in message.get("tool_calls", [])
        ]
        return LLMResponse(text=message.get("content") or "", tool_calls=calls,
                           stop_reason=choice.get("finish_reason", ""), raw=data)

    def _chat_ollama(self, messages: list[dict], tools: list[ToolSpec] | None) -> LLMResponse:
        # Ollama has no system role; fold it into the first user message.
        folded: list[dict] = []
        system_text = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
        if system_text:
            first_user = next((i for i, m in enumerate(messages) if m["role"] == "user"), None)
            if first_user is not None:
                messages = list(messages)
                merged = dict(messages[first_user])
                merged["content"] = system_text + "\n\n" + merged["content"]
                messages[first_user] = merged
            else:
                messages = [{"role": "user", "content": system_text}] + [
                    m for m in messages if m["role"] != "system"
                ]
        for m in messages:
            if m["role"] != "system":
                folded.append(m)
        messages = folded
        body: dict = {
            "model": self.config.model,
            "messages": messages,
            "stream": False,
        }
        if tools:
            body["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.parameters,
                    },
                }
                for t in tools
            ]
        data = self._post(
            self._url(self.config.base_url, "api/chat"),
            headers={"Content-Type": "application/json"},
            payload=body,
        )
        message = data.get("message", {})
        calls = [
            {
                "id": c.get("id", c.get("name", "")),
                "name": c.get("name", c.get("function", {}).get("name", "")),
                "arguments": c.get("arguments", c.get("function", {}).get("arguments", {})),
            }
            for c in message.get("tool_calls", [])
        ]
        return LLMResponse(text=message.get("content") or "", tool_calls=calls,
                           stop_reason=data.get("done_reason", ""), raw=data)

    # ------------------------------------------------------------------ http

    def _post(self, url: str, headers: dict, payload: dict, timeout: int = 120) -> dict:
        # A named User-Agent matters: NVIDIA NIM sits behind Cloudflare and
        # rejects the default "Python-urllib" signature with HTTP 403 (err 1010).
        headers = {"User-Agent": USER_AGENT, **headers}
        request = urllib.request.Request(
            url, data=json.dumps(payload).encode("utf-8"),
            headers=headers, method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
            log("llm_http_error", status=exc.code, detail=detail)
            raise LLMError(f"LLM HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            log("llm_unreachable", url=url)
            raise LLMError(
                f"LLM endpoint unreachable ({url}): {exc.reason}. "
                "For ollama, ensure `ollama serve` is running."
            ) from exc

    # ------------------------------------------------------------- utilities

    def ping(self, timeout: int = 20) -> dict:
        """Prove the brain actually answers: one tiny real request.

        'A key is configured' is not 'the brain works' — keys can be
        mistyped, out of credits, rate-limited, or pointed at a dead
        endpoint. This makes the difference observable. Returns
        {"ok": bool, "latency_ms": int, "detail": str}.
        """
        start = time.monotonic()
        try:
            response = self.chat([{
                "role": "user",
                "content": "Reply with exactly: online",
            }])
        except LLMError as exc:
            return {"ok": False, "latency_ms": 0, "detail": str(exc)[:300]}
        latency = int((time.monotonic() - start) * 1000)
        text = (response.text or "").strip()
        if not text:
            return {"ok": False, "latency_ms": latency,
                    "detail": "empty response from model"}
        return {"ok": True, "latency_ms": latency, "detail": text[:120]}

    def complete_text(self, prompt: str, system: str = "") -> str:
        """Simple one-shot text completion (no tools)."""
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        response = self.chat(messages)
        return response.text


def get_client() -> LLMClient | None:
    """Return the primary configured client, or None in rule-based mode."""
    clients = get_clients()
    return clients[0] if clients else None


def get_clients() -> list[LLMClient]:
    """All configured clients in fallback priority order.

    The primary provider is whatever load_config() resolves; additional
    providers with keys present become fallbacks for rate limits/quota
    (e.g. NVIDIA credits exhausted -> Groq -> Gemini).
    Keys from config/.env and agent_config.yaml are included here too.
    """
    primary = load_config()
    if not primary.enabled:
        return []
    clients = [LLMClient(primary)]
    seen = {primary.provider}
    for name, env_vars in ENV_PROVIDERS:
        if name in seen:
            continue
        key = ""
        for env_var in env_vars:
            key = os.environ.get(env_var, "").strip()
            if key:
                break
        if not key:
            continue
        try:
            cfg = LLMConfig(
                provider=name,
                api_key=key,
                model=DEFAULT_MODELS[name],
                base_url=DEFAULT_BASE_URLS[name],
            )
            clients.append(LLMClient(cfg))
            seen.add(name)
        except LLMError:
            continue
    return clients
