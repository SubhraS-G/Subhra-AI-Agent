"""Local web UI server — standard library only (http.server + threading).

Endpoints (all JSON, all local):
    GET  /api/status         face library, brands, memory, LLM provider
    GET  /api/packages       list thumbnail packages
    GET  /api/package?name=  one package (markdown)
    GET  /api/preferences    learned preferences
    GET  /api/search?q=      memory search
    POST /api/chat           {"message": str, "history": [...]} -> reply + tool trace
    POST /api/learn          {"rule": str, "scope": str} -> record correction

Static: / (UI), /style.css, /app.js, /manifest.webmanifest, /sw.js,
        /icon-192.png + /icon-512.png (installable taskbar app — PWA).

Start: python -m cli serve  (then open http://127.0.0.1:8756)
"""

from __future__ import annotations

import json
import secrets
import struct
import sys
import threading
import time
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import urllib.request
from urllib.parse import parse_qs, urlparse

from system.agents.creative.face_library import FaceLibrary
from system.brands import BrandRegistry, load_brand_identity
from system.logging_utils import log
from system.orchestrator import Orchestrator
from system.paths import AGENT_FULL_NAME, AGENT_NAME, AGENT_ROOT, THUMBNAIL_SYSTEM

SERVER_NAME = AGENT_NAME
HOST = "127.0.0.1"
DEFAULT_PORT = 8756

# ------------------------------------------------------------- LAN sharing
MAX_KEY_ATTEMPTS = 10            # failed pairing attempts before a cool-off
KEY_COOLDOWN_S = 300.0           # window after which failed tries expire
PAIRING_HEADER = "X-SURA-Key"


def generate_pairing_key() -> str:
    """Short, human-typeable pairing key (no ambiguous characters)."""
    alphabet = "23456789abcdefghjkmnpqrstuvwxyz"
    return "".join(secrets.choice(alphabet) for _ in range(8))


def sanitize_key(raw: str) -> str:
    return "".join(ch for ch in (raw or "").lower() if ch.isalnum())


def lan_ip() -> str:
    """Best-effort LAN IP of this machine (no traffic leaves the host)."""
    import socket

    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def lan_urls(port: int) -> list[str]:
    """URLs a phone on the same Wi-Fi would use; primary interface first."""
    import socket

    urls: list[str] = []
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip and not ip.startswith("127.") and f"http://{ip}:{port}" not in urls:
                urls.append(f"http://{ip}:{port}")
    except OSError:
        pass
    primary = f"http://{lan_ip()}:{port}"
    if primary not in urls:
        urls.insert(0, primary)
    return urls


# ---------------------------------------------------------------- Tailscale

_ts_cache: dict = {}   # {"at", "info"} — `tailscale status` is a subprocess; cache 15 s


def tailscale_status() -> dict:
    """Detect a running Tailscale and report this machine's tailnet identity.

    Best-effort and read-only: runs `tailscale status --json` when the binary
    exists. Never raises — an absent or stopped Tailscale is a normal state,
    not an error. The tailnet gives SURA a stable address that works from ANY
    network (mobile data included), with the pairing key still enforced.
    """
    import shutil
    import subprocess

    now = time.monotonic()
    if _ts_cache and now - _ts_cache["at"] < 15:
        return _ts_cache["info"]
    exe = shutil.which("tailscale")
    if not exe:
        info = {"installed": False, "running": False}
    else:
        try:
            out = subprocess.run([exe, "status", "--json"], capture_output=True,
                                 text=True, timeout=10, check=False).stdout
            data = json.loads(out or "{}")
        except (OSError, ValueError, subprocess.SubprocessError):
            info = {"installed": True, "running": False}
        else:
            backend = str(data.get("BackendState", "")).lower()
            self_dev = data.get("Self") or {}
            ips = list(self_dev.get("TailscaleIPs") or [])
            dns = str(self_dev.get("DNSName") or "").strip().strip(".")
            host = dns or (ips[0] if ips else "")
            info = {
                "installed": True,
                "running": backend == "running",
                "ip": ips[0] if ips else "",
                "dns": dns,
                "url": f"http://{host}:{DEFAULT_PORT}" if host else "",
            }
    _ts_cache.clear()
    _ts_cache.update({"at": now, "info": info})
    return info


def tailscale_urls(port: int) -> list[str]:
    """URLs SURA answers on through the tailnet (empty when not running)."""
    info = tailscale_status()
    urls: list[str] = []
    if info.get("dns"):
        urls.append(f"http://{info['dns']}:{port}")
    if info.get("ip") and f"http://{info['ip']}:{port}" not in urls:
        urls.append(f"http://{info['ip']}:{port}")
    return urls


# --------------------------------------------------------------- push inbox
# Laptop -> phone pipe: `python -m cli push "..."` drops a note in the inbox;
# every paired device's UI polls and shows it as a toast + notification.

PUSH_FILE = AGENT_ROOT / "memory" / "push_inbox.jsonl"
PUSH_MAX = 200          # inbox cap; oldest items fall off


def push_message(text: str, source: str = "laptop") -> dict:
    """Append a push note to the inbox and return it. Thread-safe."""
    PUSH_FILE.parent.mkdir(parents=True, exist_ok=True)
    item = {
        "id": secrets.token_hex(6),
        "text": text.strip()[:2000],
        "source": source,
        "at": time.time(),
    }
    with AgentRequestHandler.lock:      # serialize concurrent pushes
        try:
            lines = PUSH_FILE.read_text(encoding="utf-8").splitlines()[-(PUSH_MAX - 1):]
        except OSError:
            lines = []
        lines.append(json.dumps(item, ensure_ascii=False))
        PUSH_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    log("push_stored", id=item["id"], source=source)
    return item


def read_push_inbox(since_epoch: float = 0.0) -> list[dict]:
    """Push items newer than `since` (oldest first). Missing file = empty."""
    try:
        lines = PUSH_FILE.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    items = []
    for line in lines:
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if item.get("at", 0) > since_epoch:
            items.append(item)
    return items



# Voice -> chat screen: the wake-word listener records what was said and what
# SURA answered, and the UI polls this exactly like the push inbox. That makes
# a spoken exchange readable on the chat screen — and because the UI folds
# those turns into its own history, the conversation can continue by typing.

VOICE_TURN_FILE = AGENT_ROOT / "memory" / "voice_transcript.jsonl"
VOICE_TURN_MAX = 400        # turns kept on disk; oldest fall off


def record_voice_turn(role: str, text: str) -> dict:
    """Append one voice turn ('you' or 'sura') to the transcript. Thread-safe."""
    VOICE_TURN_FILE.parent.mkdir(parents=True, exist_ok=True)
    item = {
        "id": secrets.token_hex(6),
        "role": "you" if role == "you" else "sura",
        "text": text.strip()[:4000],
        "at": time.time(),
    }
    with AgentRequestHandler.lock:
        try:
            lines = VOICE_TURN_FILE.read_text(encoding="utf-8").splitlines()[
                -(VOICE_TURN_MAX - 1):
            ]
        except OSError:
            lines = []
        lines.append(json.dumps(item, ensure_ascii=False))
        VOICE_TURN_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return item


def read_voice_transcript(since_epoch: float = 0.0, limit: int = 50) -> list[dict]:
    """Voice turns newer than `since` (oldest first). Missing file = empty."""
    try:
        lines = VOICE_TURN_FILE.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    items = []
    for line in lines:
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if item.get("at", 0) > since_epoch:
            items.append(item)
    return items[-limit:]


def post_voice_turn(role: str, text: str, port: int = DEFAULT_PORT,
                    timeout: float = 3.0) -> bool:
    """Best-effort hand-off of a voice turn to the local server.

    Runs in the listener process (not the server), and must never disturb
    wake-word mode: every failure is swallowed, the chat screen is a bonus.
    """
    if not text.strip():
        return False
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/voice/turn",
        data=json.dumps({"role": role, "text": text}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout):
            return True
    except Exception:  # noqa: BLE001 — the chat mirror is optional
        log("voice_turn_post_failed", role=role)
        return False


_ui_dir = Path(__file__).resolve().parent.parent / "ui"


# --------------------------------------------------------------------- PWA

def make_pwa_assets() -> None:
    """Generate manifest.webmanifest, sw.js and PNG icons into ui/.

    Icons are pure-stdlib PNGs (zlib + struct): a dark rounded tile with a
    teal core and gold ring — SURA's orb. Regenerated when missing so a
    fresh checkout installs cleanly. Safe to call on every serve().
    """
    icon_paths = {size: _ui_dir / f"icon-{size}.png" for size in (192, 512)}
    for size, icon_path in icon_paths.items():
        if not icon_path.exists():
            icon_path.write_bytes(_orb_png(size))
            log("pwa_icon_generated", size=size)
    manifest = {
        "name": AGENT_FULL_NAME,
        "short_name": AGENT_NAME,
        "description": "Subhra's personal AI agent — local, private, permission-gated.",
        "start_url": "/",
        "scope": "/",
        "display": "standalone",
        "background_color": "#050a10",
        "theme_color": "#050a10",
        "icons": [
            {"src": f"/icon-{size}.png", "sizes": f"{size}x{size}", "type": "image/png",
             "purpose": "any"}
            for size in sorted(icon_paths)
        ],
    }
    (_ui_dir / "manifest.webmanifest").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )
    (_ui_dir / "sw.js").write_text(
        "// SURA service worker — app-shell cache only; /api/* always hits the live server.\n"
        "const CACHE = \"sura-shell-v2\";   // bump to force phones onto the new UI\n"
        "const SHELL = [\"/\", \"/style.css\", \"/app.js\", \"/manifest.webmanifest\"];\n"
        "self.addEventListener(\"install\", (event) => {\n"
        "  event.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));\n"
        "});\n"
        "self.addEventListener(\"activate\", (event) => {\n"
        "  event.waitUntil(caches.keys().then((keys) =>\n"
        "    Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))\n"
        "  ).then(() => self.clients.claim()));\n"
        "});\n"
        "self.addEventListener(\"fetch\", (event) => {\n"
        "  const url = new URL(event.request.url);\n"
        "  if (event.request.method !== \"GET\" || url.pathname.startsWith(\"/api/\")) return;\n"
        "  event.respondWith(\n"
        "    fetch(event.request).then((response) => {\n"
        "      const copy = response.clone();\n"
        "      caches.open(CACHE).then((c) => c.put(event.request, copy));\n"
        "      return response;\n"
        "    }).catch(() => caches.match(event.request))\n"
        "  );\n"
        "});\n",
        encoding="utf-8",
    )


def _png_chunk(tag: bytes, data: bytes) -> bytes:
    return (struct.pack(">I", len(data)) + tag + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))


def _orb_png(size: int) -> bytes:
    """Render SURA's orb as a standalone PNG using only the stdlib."""
    rows = []
    c = (size - 1) / 2
    radius = size * 0.46
    core_r = size * 0.30
    ring_in, ring_out = size * 0.38, size * 0.435
    for y in range(size):
        row = bytearray(b"\x00")
        for x in range(size):
            d = ((x - c) ** 2 + (y - c) ** 2) ** 0.5
            if d <= core_r:
                r, g, b = 56, 224, 200                      # teal core
            elif ring_in <= d <= ring_out:
                r, g, b = 255, 191, 64                      # gold ring
            elif d <= radius:
                r, g, b = 12, 26, 38                        # dark tile
            else:
                row += b"\x00\x00\x00\x00"
                continue
            row += bytes((r, g, b, 255))
        rows.append(bytes(row))
    return b"\x89PNG\r\n\x1a\n" + _png_chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)) \
        + _png_chunk(b"IDAT", zlib.compress(b"".join(rows))) + _png_chunk(b"IEND", b"")


class AgentRequestHandler(BaseHTTPRequestHandler):
    orchestrator: Orchestrator = None  # injected by serve()
    lock: threading.Lock = threading.Lock()
    pairing_key: str = ""              # "" -> open access (local-only binding)
    trust_loopback: bool = True        # the PC itself never needs the key
    _fail_log: list = []               # (time, ip) of failed pairing attempts

    # ------------------------------------------------------------- utilities

    def _send_json(self, payload, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        origin = self.headers.get("Origin", "")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if origin:
            # The installed PWA on the phone calls /api/* from its own origin.
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        self.end_headers()
        self.wfile.write(body)

    # ------------------------------------------------------------------ auth

    def _client_ip(self) -> str:
        ip = self.client_address[0]
        # Behind a local HTTPS proxy (tailscale serve) the real client IP
        # arrives in X-Forwarded-For. Only trust that header when the direct
        # peer IS the proxy (loopback) — a LAN device could otherwise spoof
        # its way past the pairing gate.
        if ip in ("127.0.0.1", "::1"):
            forwarded = self.headers.get("X-Forwarded-For", "").strip()
            if forwarded:
                return forwarded.split(",")[0].strip()
        return ip

    def _is_loopback(self) -> bool:
        return self._client_ip() in ("127.0.0.1", "::1", "localhost")

    def _authorized(self) -> bool:
        """The PC itself is trusted; remote devices need the pairing key."""
        if not self.pairing_key:
            return True
        if self.trust_loopback and self._is_loopback():
            return True
        given = sanitize_key(self.headers.get(PAIRING_HEADER, ""))
        return bool(given) and secrets.compare_digest(given, self.pairing_key)

    def _reject(self) -> None:
        now = time.monotonic()
        ip = self._client_ip()
        AgentRequestHandler._fail_log = [
            (t, seen_ip) for (t, seen_ip) in AgentRequestHandler._fail_log
            if now - t < KEY_COOLDOWN_S and seen_ip == ip
        ]
        AgentRequestHandler._fail_log.append((now, ip))
        if len(AgentRequestHandler._fail_log) >= MAX_KEY_ATTEMPTS:
            self._send_json({"error": "too many pairing attempts — wait five minutes"}, 429)
            return
        self._send_json({"error": "pairing required", "code": "pairing_required"}, 401)

    def do_OPTIONS(self) -> None:
        # CORS preflight (the installed PWA on the phone calls /api/*).
        self.send_response(204)
        origin = self.headers.get("Origin", "")
        if origin:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", f"Content-Type, {PAIRING_HEADER}")
            self.send_header("Access-Control-Max-Age", "600")
        self.end_headers()

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", "0") or 0)
        if length <= 0:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except json.JSONDecodeError:
            return {}

    def log_message(self, fmt, *args):  # quiet default logging
        pass

    # ------------------------------------------------------------------ GET

    def do_GET(self):
        parsed = urlparse(self.path)
        route = parsed.path

        if route in ("/", "/index.html"):
            return self._serve_file(_ui_dir / "index.html", "text/html; charset=utf-8")
        if route == "/style.css":
            return self._serve_file(_ui_dir / "style.css", "text/css; charset=utf-8")
        if route == "/app.js":
            return self._serve_file(_ui_dir / "app.js", "text/javascript; charset=utf-8")
        if route == "/manifest.webmanifest":
            return self._serve_file(_ui_dir / "manifest.webmanifest",
                                    "application/manifest+json; charset=utf-8")
        if route == "/sw.js":
            return self._serve_file(_ui_dir / "sw.js", "text/javascript; charset=utf-8")
        if route in ("/icon-192.png", "/icon-512.png"):
            return self._serve_file(_ui_dir / route.lstrip("/"), "image/png")

        try:
            # /api/pair is the handshake that VALIDATES a candidate key, so it
            # must be reachable before auth; everything else requires the key.
            if route != "/api/pair" and not self._authorized():
                self._reject()
                return
            if route == "/api/status":
                return self._send_json(self._status())
            if route == "/api/packages":
                return self._send_json(self._packages())
            if route == "/api/package":
                name = parse_qs(parsed.query).get("name", [""])[0]
                return self._send_json(self._package(name))
            if route == "/api/preferences":
                return self._send_json(self._preferences())
            if route == "/api/search":
                query = parse_qs(parsed.query).get("q", [""])[0]
                return self._send_json(self._search(query))
            if route == "/api/pair":
                given = parse_qs(parsed.query).get("key", [""])[0]
                return self._send_json(self._pair(given))
            if route == "/api/llm/health":
                return self._send_json(self._llm_health())
            if route == "/api/push/poll":
                since = float(parse_qs(parsed.query).get("since", ["0"])[0] or 0)
                return self._send_json({"items": read_push_inbox(since), "now": time.time()})
            if route == "/api/voice/transcript":
                query = parse_qs(parsed.query)
                tail = (query.get("tail", [""])[0] or "").strip()
                if tail:                        # first load: the recent turns
                    limit = max(1, min(200, int(tail or 20)))
                    return self._send_json({"items": read_voice_transcript(0.0, limit=limit),
                                            "now": time.time()})
                since = float(query.get("since", ["0"])[0] or 0)
                return self._send_json({"items": read_voice_transcript(since),
                                        "now": time.time()})
            if route == "/api/tailscale":
                return self._send_json(tailscale_status())
            if route == "/api/editing/plans":
                return self._send_json({"plans": self._editing_plans()})
            if route == "/api/editing/plan":
                name = parse_qs(parsed.query).get("name", [""])[0]
                return self._send_json(self._editing_plan(name))
            self._send_json({"error": "not found"}, 404)
        except Exception as exc:  # noqa: BLE001 — report, never crash the server
            log("api_error", route=route, error=str(exc))
            self._send_json({"error": str(exc)}, 500)

    def _serve_file(self, path: Path, content_type: str) -> None:
        if not path.exists():
            self._send_json({"error": f"missing ui file: {path.name}"}, 404)
            return
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    # ----------------------------------------------------------------- POST

    def do_POST(self):
        route = urlparse(self.path).path
        data = self._read_json()
        try:
            if not self._authorized():
                self._reject()
                return
            if route == "/api/chat":
                message = (data.get("message") or "").strip()
                if not message:
                    return self._send_json({"error": "message required"}, 400)
                with self.lock:
                    result = self.orchestrator.chat(
                        message, history=data.get("history") or []
                    )
                return self._send_json(result)
            if route == "/api/chat/stream":
                message = (data.get("message") or "").strip()
                if not message:
                    return self._send_json({"error": "message required"}, 400)
                return self._chat_stream(message, data.get("history") or [])
            if route == "/api/push":
                text = (data.get("text") or data.get("message") or "").strip()
                if not text:
                    return self._send_json({"error": "text required"}, 400)
                return self._send_json(push_message(text, source="laptop"))
            if route == "/api/voice/turn":
                text = (data.get("text") or "").strip()
                if not text:
                    return self._send_json({"error": "text required"}, 400)
                item = record_voice_turn(str(data.get("role") or "sura"), text)
                return self._send_json({"id": item["id"], "at": item["at"]})
            if route == "/api/learn":
                rule = (data.get("rule") or "").strip()
                if not rule:
                    return self._send_json({"error": "rule required"}, 400)
                with self.lock:
                    pref = self.orchestrator.record_preference(
                        rule, scope=data.get("scope", "general")
                    )
                return self._send_json({
                    "id": pref.id,
                    "confidence": pref.confidence,
                    "evidence_count": pref.evidence_count,
                    "message": f"Recorded '{pref.id}' at confidence={pref.confidence} (evidence={pref.evidence_count}).",
                })
            if route == "/api/set-key":
                key = (data.get("key") or "").strip()
                provider = (data.get("provider") or "").strip().lower()
                if not key:
                    return self._send_json({"error": "key required"}, 400)
                return self._send_json(self._set_key(key, provider))
            if route == "/api/editing/learn":
                text = (data.get("text") or "").strip()
                if not text:
                    return self._send_json({"error": "text required"}, 400)
                from system.agents.editing.editing_agent import EditingAgent

                with self.lock:
                    result = EditingAgent(memory=self.orchestrator.memory).handle(
                        "learn editing style", {"mode": "learn", "text": text}
                    )
                return self._send_json({
                    "saved": result.status == "completed",
                    "message": result.summary,
                })
            if route == "/api/editing/plan":
                topic = (data.get("topic") or "").strip()
                if not topic:
                    return self._send_json({"error": "topic required"}, 400)
                from system.agents.editing.editing_agent import EditingAgent

                with self.lock:
                    result = EditingAgent(memory=self.orchestrator.memory).handle(
                        f"edit plan for {topic}", {"mode": "plan", "topic": topic}
                    )
                return self._send_json({
                    "built": result.status == "completed",
                    "message": result.summary,
                    "artifacts": result.artifacts,
                })
            self._send_json({"error": "not found"}, 404)
        except Exception as exc:  # noqa: BLE001
            log("api_error", route=route, error=str(exc))
            self._send_json({"error": str(exc)}, 500)

    # -------------------------------------------------------------- streaming

    def _chat_stream(self, message: str, history: list[dict]) -> None:
        """POST /api/chat/stream — newline-delimited JSON events.

        Each line is one event: {"type": "text", "text": chunk} while the
        reply forms, then {"type": "done", "reply", "tool_trace", "mode"}.
        NDJSON over chunked HTTP works everywhere urllib, curl and fetch do —
        no EventSource/POST gymnastics — and the phone sees the first words
        in roughly one round trip instead of after the full generation.
        """
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")
        origin = self.headers.get("Origin", "")
        if origin:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        self.end_headers()
        try:
            # The global lock is taken per event, not for the whole stream:
            # other endpoints stay responsive while tokens flow.
            for event in self.orchestrator.chat_stream(message, history=history):
                with self.lock:
                    line = json.dumps(event, ensure_ascii=False).encode("utf-8")
                    self.wfile.write(line + b"\n")
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass                                    # phone walked away mid-reply
        except Exception as exc:  # noqa: BLE001
            log("chat_stream_failed", error=str(exc)[:200])
            try:
                self.wfile.write(json.dumps(
                    {"type": "error", "reply": f"stream failed: {exc}"}
                ).encode("utf-8") + b"\n")
            except OSError:
                pass

    # --------------------------------------------------------------- handlers

    def _status(self) -> dict:
        from system.voice.wake import verifier_status

        try:
            voice = verifier_status()
        except Exception:  # noqa: BLE001 — voice layer optional
            voice = {"enrolled": False}
        faces = FaceLibrary().status()
        brands = []
        for brand in BrandRegistry().list_brands():
            identity = load_brand_identity(brand)
            brands.append({
                "slug": brand.slug,
                "name": brand.name,
                "identity_filled": identity["filled"],
            })
        from system.llm.client import load_config

        cfg = load_config()
        try:
            from system.agents.editing.editing_agent import StyleBook, list_plans

            editing = {
                "styles": StyleBook().names(),
                "plans": len(list_plans(50)),
            }
        except Exception:  # noqa: BLE001 — editing layer optional
            editing = {"styles": [], "plans": 0}
        try:
            store = self.orchestrator.memory
            stats = store.stats() if hasattr(store, "stats") else {}
            prefs_count = len(store.list_preferences())
            if "preferences" in stats and stats["preferences"]:
                prefs_count = sum(stats["preferences"].values())
        except Exception:  # noqa: BLE001
            stats, prefs_count = {}, 0
        return {
            "agent": SERVER_NAME,
            "agent_full": AGENT_FULL_NAME,
            "shared": bool(self.pairing_key),
            "llm": {
                "provider": cfg.provider or None,
                "model": cfg.model or None,
                "enabled": cfg.enabled,
            },
            "face_library": {
                "total": faces["total"],
                "by_expression": faces["by_expression"],
                "missing": faces["missing_expressions"],
            },
            "brands": brands,
            "memory": {
                "preferences": prefs_count,
                "stats": stats,
            },
            "voice": {
                "enrolled": bool(voice.get("enrolled")),
                "listening": bool(voice.get("listening")),
                "owner": voice.get("owner"),
            },
            "editing": editing,
        }

    @staticmethod
    def _packages() -> dict:
        out_dir = THUMBNAIL_SYSTEM / "output"
        packages = []
        if out_dir.exists():
            for day_dir in sorted(out_dir.iterdir(), reverse=True):
                if not day_dir.is_dir():
                    continue
                pkg = day_dir / "package.md"
                if pkg.exists():
                    packages.append({
                        "name": day_dir.name,
                        "modified": pkg.stat().st_mtime,
                        "age": pkg.stat().st_mtime,
                    })
        return {"packages": packages[:30]}

    @staticmethod
    def _package(name: str) -> dict:
        if not name or "/" in name or "\\" in name or ".." in name:
            return {"error": "invalid package name"}
        path = THUMBNAIL_SYSTEM / "output" / name / "package.md"
        if not path.exists():
            return {"error": "package not found"}
        return {
            "name": name,
            "markdown": path.read_text(encoding="utf-8"),
            "modified": path.stat().st_mtime,
        }

    def _preferences(self) -> dict:
        store = self.orchestrator.memory
        return {
            "preferences": [
                {
                    "id": p.id,
                    "rule": p.rule,
                    "scope": p.scope,
                    "confidence": p.confidence,
                    "evidence": p.evidence_count,
                }
                for p in store.list_preferences()
            ]
        }

    def _search(self, query: str) -> dict:
        if not query.strip():
            return {"results": []}
        store = self.orchestrator.memory
        if hasattr(store, "search"):
            return {"results": store.search(query, limit=8)}
        return {"results": [], "note": "search requires the SQLite memory store"}

    @staticmethod
    def _llm_health() -> dict:
        """Live brain check: does the configured provider actually answer?

        Cached for 60 s so the UI can poll without hammering the API.
        'A key exists' and 'the brain works' are different facts.
        """
        import time as _time

        from system.llm.client import LLMClient, LLMError, load_config

        now = _time.monotonic()
        cached = AgentRequestHandler._llm_health_cache
        if cached and now - cached["at"] < 60:
            return cached["payload"]
        payload: dict
        try:
            cfg = load_config()
        except LLMError as exc:
            payload = {"ok": False, "latency_ms": 0, "detail": str(exc)[:200]}
        else:
            if not cfg.enabled:
                payload = {"ok": False, "latency_ms": 0, "detail": "no provider configured"}
            else:
                try:
                    payload = LLMClient(cfg).ping()
                except LLMError as exc:
                    payload = {"ok": False, "latency_ms": 0, "detail": str(exc)[:200]}
        AgentRequestHandler._llm_health_cache = {"at": now, "payload": payload}
        return payload

    _llm_health_cache: dict = {}

    # ------------------------------------------------------------------ pair

    def _pair(self, given: str) -> dict:
        """Tell the phone UI whether its stored key matches."""
        expected = self.pairing_key
        given_clean = sanitize_key(given)
        return {
            "ok": bool(
                expected
                and given_clean
                and secrets.compare_digest(given_clean, expected)
            ),
            "agent": SERVER_NAME,
            "agent_full": AGENT_FULL_NAME,
        }

    @staticmethod
    def _editing_plans() -> list[dict]:
        from system.agents.editing.editing_agent import list_plans

        return list_plans()

    @staticmethod
    def _editing_plan(name: str) -> dict:
        from system.agents.editing.editing_agent import read_plan

        return read_plan(name)

    def _set_key(self, key: str, provider: str) -> dict:
        """Persist an LLM API key from the UI (first free provider is guessed)."""
        from system.llm.client import LLMClient, LLMError, load_config, save_key

        if self.pairing_key and not self._is_loopback():
            # A phone on the LAN must never be able to replace the brain's API key.
            log("set_key_refused_remote", ip=self._client_ip())
            return {"error": "API keys can only be set from the PC itself (open the UI on this computer)."}

        if not provider:
            # Guess from the key's shape: nvapi- -> nvidia, gsk_ -> groq.
            if key.startswith("nvapi-") or key.startswith("nvapi_"):
                provider = "nvidia"
            elif key.startswith("gsk_"):
                provider = "groq"
            elif key.startswith("AI"):
                provider = "google"
            else:
                provider = "nvidia"  # Subhra's primary provider
        try:
            path = save_key(provider, key)
            cfg = load_config()
        except LLMError as exc:
            return {"error": str(exc)}
        # A new key invalidates any cached health verdict.
        AgentRequestHandler._llm_health_cache = {}
        # Prove the key works before claiming the brain is online.
        health = {"ok": False, "detail": "not checked"}
        try:
            health = LLMClient(cfg).ping()
        except LLMError as exc:
            health = {"ok": False, "detail": str(exc)[:200]}
        verdict = (
            f"Brain verified online ({health.get('latency_ms', '?')} ms)."
            if health.get("ok")
            else f"Key saved, but the brain did NOT answer: {health.get('detail', 'unknown')}"
        )
        return {
            "provider": cfg.provider,
            "model": cfg.model,
            "verified": bool(health.get("ok")),
            "health": health,
            "message": (f"API key saved for {cfg.provider} ({cfg.model}). "
                        f"{verdict} Stored in {path.name} (gitignored)."),
        }


# A client that walks away mid-response (browser reload, phone sleeping, an
# interrupted curl) is normal traffic. socketserver answers it with a full
# traceback on stderr, which used to fill the autostart log with fake errors.
CLIENT_GONE_ERRORS = (ConnectionAbortedError, ConnectionResetError,
                      BrokenPipeError, TimeoutError)


class QuietHTTPServer(ThreadingHTTPServer):
    """ThreadingHTTPServer that logs disconnected clients in one line."""

    def handle_error(self, request, client_address) -> None:
        error = sys.exc_info()[1]
        if isinstance(error, CLIENT_GONE_ERRORS):
            log("client_disconnected", client=str(client_address),
                error=type(error).__name__)
            return
        super().handle_error(request, client_address)


def serve(
    host: str = HOST,
    port: int = DEFAULT_PORT,
    share: bool = False,
    pairing_key: str = "",
    qr: bool = False,
    tunnel: bool = False,
) -> None:
    """Run SURA's web UI.

    share=False -> loopback only (the historical, private mode).
    share=True  -> bind to the LAN, generate a pairing key, and require it
    from every non-loopback request so only devices holding the key can use
    the agent. The PC itself (loopback) is always trusted.
    tunnel=True -> also print the Tailscale address (reachable from anywhere,
    on any network, once Tailscale is signed in on both devices). The pairing
    key is enforced on tailnet requests exactly like on the LAN.
    """
    try:
        make_pwa_assets()
    except Exception as exc:  # noqa: BLE001 — PWA files are cosmetic, never fatal
        log("pwa_assets_failed", error=str(exc))

    if share and not pairing_key:
        pairing_key = generate_pairing_key()
    bind_host = "0.0.0.0" if share else host
    trust_loopback = share  # in private mode nothing remote can connect anyway

    orchestrator = Orchestrator()
    handler = type("BoundHandler", (AgentRequestHandler,), {
        "orchestrator": orchestrator,
        "pairing_key": sanitize_key(pairing_key),
        "trust_loopback": trust_loopback,
    })
    httpd = QuietHTTPServer((bind_host, port), handler)
    log("server_started", host=bind_host, port=port, shared=share)
    print(f"{AGENT_FULL_NAME}  ->  http://{host}:{port}   (Ctrl+C to stop)")
    if share:
        urls = lan_urls(port)
        print()
        print("PHONE ACCESS — same Wi-Fi, open any of:")
        for url in urls[:3]:
            print(f"    {url}   (pairing key: {pairing_key})")
        print()
        print("  • The key is required once per device; the phone remembers it.")
        print("  • 'python -m cli serve' (without --share) is private to this PC.")
        print("  • The API key box is PC-only while shared — your phone cannot change it.")
        if qr:
            _print_qr(f"{urls[0]}/?key={pairing_key}")
    if tunnel:
        ts = tailscale_status()
        print()
        if not ts.get("installed"):
            print("TAILSCALE — not installed on this PC yet (free, 3 devices+):")
            print("  1. Install:  winget install tailscale.tailscale   (then sign in)")
            print("  2. Install Tailscale on your phone and sign in to the SAME account")
            print("  3. Re-run:   python -m cli serve --share --tunnel --qr")
            print("  From then on SURA answers at a stable address from ANY network,")
            print("  and the pairing key above stays required — unknown devices get 401.")
        elif not ts.get("running"):
            print("TAILSCALE — installed but not connected. Start it, then reopen SURA.")
        else:
            print("TAILSCALE — works from ANYWHERE (any Wi-Fi, mobile data), not just home:")
            for url in tailscale_urls(port):
                print(f"    {url}   (pairing key: {pairing_key})")
            print("  • Same pairing key as the LAN; your phone needs it only once.")
            print("  • Tailscale phones reach this PC by its tailnet name (MagicDNS).")
            if qr:
                primary = tailscale_urls(port)
                if primary:
                    _print_qr(f"{primary[0]}/?key={pairing_key}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        httpd.server_close()


def _print_qr(url: str) -> None:
    """Print a scannable QR code if the optional segno package is present."""
    try:
        import segno
    except ImportError:
        print("  tip: pip install segno   → 'python -m cli serve --share --qr' prints a scan-to-pair QR code")
        return
    qr = segno.make(url, error="m")
    print("  scan to pair:")
    qr.terminal(compact=True)
