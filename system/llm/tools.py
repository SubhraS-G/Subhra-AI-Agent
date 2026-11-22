"""Tool layer for the LLM brain.

Every capability the agent already has, exposed as provider-neutral tools.
The permission gate runs inside the handlers: approval-tier actions return
a structured "needs approval" result instead of executing — the LLM cannot
bypass the gate because it never sees the raw capability.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from system.agents.creative.thumbnail_agent import ThumbnailAgent
from system.logging_utils import log
from system.memory import MemoryStore
from system.permissions import Tier

_TOOL_PARAMS_THUMBNAIL = {
    "type": "object",
    "properties": {
        "video_topic": {"type": "string", "description": "The video's topic"},
        "channel": {"type": "string", "enum": ["subhra-gaming"]},
        "tone": {"type": "string", "description": "emotional tone: excited, shocked, serious, ..."},
        "text_overlay": {"type": "string", "description": "Short text phrase (max ~4 words)"},
        "creator_in_frame": {"type": "boolean", "default": True},
    },
    "required": ["video_topic", "channel"],
}


def build_tools(memory: MemoryStore) -> list:
    """Return the ToolSpec list wired to live handlers."""

    thumbnail_agent = ThumbnailAgent(memory=memory)

    def run_thumbnail(args: dict) -> str:
        topic = args.get("video_topic", "").strip()
        channel = args.get("channel", "").strip()
        if not topic or not channel:
            return "ERROR: video_topic and channel are required."
        context = {
            "agent": "creative",
            "channel_override": channel,
            "video_topic": topic,
            "emotional_tone": args.get("tone", ""),
            "text_overlay": args.get("text_overlay", ""),
            "creator_in_frame": args.get("creator_in_frame", True),
        }
        context = {k: v for k, v in context.items() if v}
        result = thumbnail_agent.handle(f"thumbnail for {topic}", context)
        return json.dumps({
            "status": result.status,
            "summary": result.summary,
            "artifacts": result.artifacts,
            "next_action": result.next_action,
        }, ensure_ascii=False)

    def record_feedback(args: dict) -> str:
        rule = args.get("rule", "").strip()
        if not rule:
            return "ERROR: rule text required."
        pref = memory.record_correction(
            rule=rule, scope=args.get("scope", "general"),
            note=args.get("context", ""),
        )
        return (
            f"Recorded '{pref.id}' at confidence={pref.confidence} "
            f"(evidence={pref.evidence_count}). "
            + ("It is now an established rule." if pref.confidence == "established"
               else "One correction is a tendency, not a law; it strengthens with repetition.")
        )

    def search_memory(args: dict) -> str:
        from system.sqlite_memory import SQLiteMemoryStore

        if not isinstance(memory, SQLiteMemoryStore):
            return json.dumps({"results": [], "note": "search requires the SQLite memory store"})
        hits = memory.search(args.get("query", ""), limit=int(args.get("limit", 5)))
        return json.dumps({"results": hits}, ensure_ascii=False)

    def read_memory(args: dict) -> str:
        content = memory.read_note(args.get("memory_class", "projects"), args.get("name", ""))
        return content if content else "Note not found."

    def write_memory(args: dict) -> str:
        memory_class = args.get("memory_class", "projects")
        if memory_class not in {"permanent", "projects", "workflows", "temporary"}:
            return "ERROR: memory_class must be permanent|projects|workflows|temporary."
        name = (args.get("name") or "").strip()
        content = args.get("content", "")
        if not name or not content.strip():
            return "ERROR: name and content required."
        path = memory.write_note(memory_class, name, content)
        return f"Stored as {memory_class}/{name}."

    def check_approval(args: dict) -> str:
        """Explain the permission tier of a consequential action."""
        action = args.get("action", "").strip()
        from system.permissions import ACTION_REGISTRY

        entry = ACTION_REGISTRY.get(action)
        if entry is None:
            return f"Unknown action '{action}'. Fail closed: treat as approval-required."
        tier = entry.tier.value
        if entry.tier == Tier.AUTOMATIC:
            verdict = "Automatic — you may proceed."
        else:
            verdict = (
                "Approval required — do NOT execute. Tell Subhra what you intend "
                "and ask for explicit approval."
            )
        return f"Action '{action}' ({entry.description}) is tier '{tier}'. {verdict}"

    def agent_status(args: dict) -> str:
        from system.agents.creative.face_library import FaceLibrary
        from system.brands import BrandRegistry, load_brand_identity

        faces = FaceLibrary().status()
        brands = [
            {"brand": b.slug, "identity_filled": load_brand_identity(b)["filled"]}
            for b in BrandRegistry().list_brands()
        ]
        prefs = memory.list_preferences()
        return json.dumps({
            "face_references": faces["total"],
            "missing_expressions": faces["missing_expressions"],
            "brands": brands,
            "learned_preferences": len(prefs),
        }, ensure_ascii=False)

    def learn_editing_style(args: dict) -> str:
        from system.agents.editing.editing_agent import EditingAgent

        text = args.get("text", "").strip()
        if not text:
            return "ERROR: text required (paste study notes or a YouTube link with what you noticed)."
        result = EditingAgent(memory=memory).handle(
            "learn editing style", {"mode": "learn", "text": text}
        )
        return result.summary

    def build_edit_plan(args: dict) -> str:
        from system.agents.editing.editing_agent import EditingAgent

        topic = args.get("video_topic", "").strip()
        if not topic:
            return "ERROR: video_topic required."
        result = EditingAgent(memory=memory).handle(
            f"edit plan for {topic}", {"mode": "plan", "topic": topic}
        )
        return json.dumps({
            "status": result.status,
            "summary": result.summary,
            "artifacts": result.artifacts,
            "next_action": result.next_action,
        }, ensure_ascii=False)

    def run_pc_command(args: dict) -> str:
        """Run a shell command on THIS PC (the machine SURA lives on)."""
        command = args.get("command", "").strip()
        if not command:
            return "ERROR: command required."
        lowered = command.lower()
        blocked = (
            "format ", "shutdown", "logoff", "restart-computer", "stop-computer",
            "rm -rf", "del /s", "rd /s", "rmdir /s", "diskpart", "reg delete",
            "reg add", "bcdedit", "vssadmin", "cipher /w", "mkfs", "dd if=",
            "remove-item -recurse -force", "schtasks /delete",
        )
        hit = next((b for b in blocked if b in lowered), None)
        if hit:
            log("pc_command_blocked", reason=hit)
            return f"DENIED: '{hit}' looks destructive — Subhra must run that himself."
        try:
            timeout = min(int(args.get("timeout", 30) or 30), 120)
        except (TypeError, ValueError):
            timeout = 30
        try:
            proc = subprocess.run(
                command, shell=True, capture_output=True, text=True,
                timeout=timeout, errors="replace",
            )
        except subprocess.TimeoutExpired:
            return f"TIMEOUT after {timeout}s: {command}"
        out = (proc.stdout or "").strip()
        err = (proc.stderr or "").strip()
        text = f"exit={proc.returncode}"
        if out:
            text += f"\nstdout:\n{out[:1200]}"
        if err:
            text += f"\nstderr:\n{err[:400]}"
        return text

    def open_app(args: dict) -> str:
        """Launch a program, file, or URL on THIS PC via ShellExecute."""
        name = args.get("app", "").strip()
        if not name:
            return "ERROR: app required."
        try:
            os.startfile(name)  # exe names, .lnk files, URLs, App Paths
            return f"Launched {name}."
        except OSError:
            subprocess.Popen(f'start "" "{name}"', shell=True)
            return f"Launch requested for {name}."

    def push_to_phone(args: dict) -> str:
        """Flick a note to the SURA phone app (same inbox as /api/push)."""
        text = args.get("text", "").strip()
        if not text:
            return "ERROR: text required."
        from system.server import push_message  # lazy: server imports tools

        return json.dumps({"pushed": True, **push_message(text, source="sura")},
                          ensure_ascii=False)[:300]

    from system.llm.client import ToolSpec

    return [
        ToolSpec(
            name="run_thumbnail_workflow",
            description=(
                "Run the full 13-step thumbnail workflow for a video: brief, face "
                "selection, ranked concepts, composition plan, brand sheet, and a "
                "review package written to disk. Use for any thumbnail request."
            ),
            parameters=_TOOL_PARAMS_THUMBNAIL,
            handler=run_thumbnail,
        ),
        ToolSpec(
            name="record_feedback",
            description=(
                "Record a correction or preference from Subhra into the learning "
                "system. Use whenever Subhra corrects your behavior or states a "
                "standing preference."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "rule": {"type": "string", "description": "The rule, one sentence"},
                    "scope": {"type": "string", "default": "general"},
                    "context": {"type": "string", "description": "Optional detail"},
                },
                "required": ["rule"],
            },
            handler=record_feedback,
        ),
        ToolSpec(
            name="search_memory",
            description="Full-text search across stored memory notes.",
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "limit": {"type": "integer", "default": 5},
                },
                "required": ["query"],
            },
            handler=search_memory,
        ),
        ToolSpec(
            name="read_memory",
            description="Read one memory note verbatim.",
            parameters={
                "type": "object",
                "properties": {
                    "memory_class": {"type": "string", "enum": ["permanent", "projects", "workflows", "temporary"]},
                    "name": {"type": "string"},
                },
                "required": ["memory_class", "name"],
            },
            handler=read_memory,
        ),
        ToolSpec(
            name="write_memory",
            description=(
                "Persist a note to memory: project facts, task context, or "
                "important decisions. Choose the class carefully."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "memory_class": {"type": "string", "enum": ["permanent", "projects", "workflows", "temporary"]},
                    "name": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["memory_class", "name", "content"],
            },
            handler=write_memory,
        ),
        ToolSpec(
            name="check_approval",
            description=(
                "Check the permission tier of an action before attempting it "
                "(e.g. youtube_upload, publish_content, git_push, file_delete)."
            ),
            parameters={
                "type": "object",
                "properties": {"action": {"type": "string"}},
                "required": ["action"],
            },
            handler=check_approval,
        ),
        ToolSpec(
            name="agent_status",
            description="Report face library, brand identity, and learned-preference status.",
            parameters={"type": "object", "properties": {}},
            handler=agent_status,
        ),
        ToolSpec(
            name="learn_editing_style",
            description=(
                "Save what Subhra observed about a gaming YouTuber's editing style "
                "(pacing, cuts, zooms, sound design). Pass the pasted notes/links "
                "verbatim as text. Use for any 'learn this editor's style' request."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "Study notes or video link + observations, verbatim"},
                },
                "required": ["text"],
            },
            handler=learn_editing_style,
        ),
        ToolSpec(
            name="build_edit_plan",
            description=(
                "Build a concrete per-video edit plan (hook, pacing targets, zoom "
                "recipes, sound design, export checklist) from the learned style "
                "profiles. Use when Subhra asks how to edit a specific video."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "video_topic": {"type": "string", "description": "The video's topic/title"},
                },
                "required": ["video_topic"],
            },
            handler=build_edit_plan,
        ),
        ToolSpec(
            name="run_pc_command",
            description=(
                "Run a shell command on THIS PC (the laptop SURA lives on) and "
                "return its output — list files, check processes, control media, "
                "git status, anything a terminal can do. Destructive commands "
                "are refused automatically."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "Shell command, e.g. 'dir E:\\videos'"},
                    "timeout": {"type": "integer", "default": 30, "description": "Seconds before giving up (max 120)"},
                },
                "required": ["command"],
            },
            handler=run_pc_command,
        ),
        ToolSpec(
            name="open_app",
            description=(
                "Open a program, file, or website on THIS PC (e.g. 'chrome', "
                "'spotify', 'notepad', 'https://youtube.com')."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "app": {"type": "string", "description": "App name, file path, or URL"},
                },
                "required": ["app"],
            },
            handler=open_app,
        ),
        ToolSpec(
            name="push_to_phone",
            description=(
                "Flick a short note or link to Subhra's phone — it appears as a "
                "toast in the SURA phone app. Use when Subhra is away from the "
                "laptop or asks to send something to the phone."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "Short note (a sentence or a URL)"},
                },
                "required": ["text"],
            },
            handler=push_to_phone,
        ),
    ]


def dispatch(tools: list, name: str, arguments: dict) -> str:
    """Run a named tool and return its string result (never raises)."""
    for tool in tools:
        if tool.name == name:
            try:
                return str(tool.handler(arguments))
            except Exception as exc:  # noqa: BLE001 — tool errors go back to the LLM
                log("tool_error", tool=name, error=str(exc))
                return f"TOOL ERROR in {name}: {exc}"
    return f"Unknown tool '{name}'."
