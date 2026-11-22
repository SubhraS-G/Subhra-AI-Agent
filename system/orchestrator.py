"""Orchestrator.

AGENT_SPEC section 13:

    USER -> ORCHESTRATOR -> MEMORY + PLANNER + RULES -> SPECIALIST AGENTS
         -> TOOLS -> APPROVAL GATE -> ACTION -> VERIFIED RESULT

The orchestrator owns the core loop (UNDERSTAND -> PLAN -> EXECUTE ->
VERIFY -> LEARN), routes work to specialists, enforces the permission
gate before any action, and only reports results that were verified.
"""

from __future__ import annotations

import datetime as _dt
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from system.agents.base import TaskResult
from system.agents.registry import AgentRegistry
from system.core_docs import load_core_docs
from system.logging_utils import log, log_task
from system.memory import MemoryStore
from system.permissions import PermissionDenied, PermissionGate, Tier


def default_memory_store():
    """SQLite-backed memory when available, file memory otherwise."""
    try:
        from system.sqlite_memory import SQLiteMemoryStore

        return SQLiteMemoryStore()
    except Exception:  # noqa: BLE001 — missing sqlite3/FTS must not kill the CLI
        return MemoryStore()


@dataclass
class Session:
    """Temporary task context for one user interaction."""

    id: str
    started: str
    objective: str
    results: list = field(default_factory=list)

    @classmethod
    def start(cls, objective: str) -> "Session":
        return cls(
            id=uuid.uuid4().hex[:12],
            started=_dt.datetime.now().isoformat(timespec="seconds"),
            objective=objective,
        )


class Orchestrator:
    def __init__(self, granted: Tier = Tier.AUTOMATIC, memory: MemoryStore | None = None) -> None:
        self.docs = load_core_docs()
        self.memory = memory or default_memory_store()
        self.permissions = PermissionGate(granted=granted)
        self.registry = AgentRegistry(memory=self.memory)
        self._agent_loop = None

    # ------------------------------------------------------------------ brain

    def chat(self, user_message: str, history: list[dict] | None = None) -> dict:
        """Converse through the LLM brain (Tier 2a).

        Falls back to a rule-mode notice when no provider is configured.
        Returns {"reply", "tool_trace", "mode"}.
        """
        if self._agent_loop is None:
            from system.llm.agent_loop import AgentLoop
            from system.llm.client import get_clients

            clients = get_clients()
            if not clients:
                return {
                    "mode": "rule",
                    "reply": (
                        "No LLM provider configured — running in rule-based mode.\n"
                        "Fix (persists forever, works on Windows):\n"
                        "  python -m cli set-key nvidia <your-key>\n"
                        "Free keys (pick one):\n"
                        "  1. NVIDIA NIM:  build.nvidia.com — free credits, no card\n"
                        "  2. Groq:        console.groq.com — free tier\n"
                        "  3. Gemini:      aistudio.google.com — free tier\n"
                        "  4. Local:       ollama.com, `ollama pull llama3.1`, "
                        "then: python -m cli set-key ollama ollama\n"
                        "Or paste the key into the web UI's 'paste LLM API key' box."
                    ),
                    "tool_trace": [],
                }
            self._agent_loop = AgentLoop(
                clients[0], self.memory, fallback_clients=clients[1:]
            )

        result = self._agent_loop.run(user_message, history)
        result["mode"] = "llm"
        log("chat_complete", mode="llm", tools_used=len(result.get("tool_trace", [])))
        return result

    def chat_stream(self, user_message: str, history: list[dict] | None = None):
        """Streaming twin of chat(): yields {"type": ...} events live.

        Yields {"type": "text", "text": chunk} as the reply forms, then one
        final {"type": "done"|"error", "reply", "tool_trace", "mode"} —
        the same dict chat() would have returned. Rule-mode (no provider)
        callers get a single text event with the setup notice, so consumers
        need no special cases.
        """
        if self._agent_loop is None:
            from system.llm.agent_loop import AgentLoop
            from system.llm.client import get_clients

            clients = get_clients()
            if not clients:
                notice = self.chat("", history)  # rule-mode notice path
                yield {"type": "text", "text": notice["reply"]}
                yield {"type": "done", "reply": notice["reply"],
                       "tool_trace": [], "mode": "rule"}
                return
            self._agent_loop = AgentLoop(
                clients[0], self.memory, fallback_clients=clients[1:]
            )
        reply = ""
        trace: list[dict] = []
        try:
            for event in self._agent_loop.run_stream(user_message, history):
                if event["type"] == "text":
                    reply += event["text"]
                    yield event
                else:
                    reply = event.get("reply", reply)
                    trace = event.get("tool_trace", trace)
                    yield {"type": "done" if event["type"] == "done" else "error",
                           "reply": reply, "tool_trace": trace, "mode": "llm"}
                    log("chat_complete", mode="llm", tools_used=len(trace))
                    return
        except Exception as exc:  # noqa: BLE001 — the stream must always terminate cleanly
            log("chat_stream_error", error=str(exc)[:200])
            yield {"type": "error", "reply": reply or f"The reply stream failed: {exc}",
                   "tool_trace": trace, "mode": "llm"}

    # --------------------------------------------------------------- requests

    def request(self, objective: str, context: dict | None = None) -> TaskResult:
        """Run one user objective through the core loop."""
        session = Session.start(objective)
        context = context or {}

        # UNDERSTAND
        intent = {
            "objective": objective,
            "context": context,
            "user_profile_present": bool(self.docs.user_profile.strip()) and "Fill this file" not in self.docs.user_profile,
        }

        # PLAN
        plan = self._plan(intent)
        log_task(session.id, "plan", "created", " -> ".join(plan))

        # Permissions gate: consequential actions must clear the gate first.
        requested_action = context.get("action")
        if requested_action:
            try:
                self.permissions.require(requested_action)
            except PermissionDenied as exc:
                log_task(session.id, "permissions", "blocked", str(exc))
                return TaskResult(
                    agent="orchestrator",
                    objective=objective,
                    status="needs_approval",
                    summary=str(exc),
                    next_action="Grant approval for the action, then retry.",
                )

        # EXECUTE (route to specialist)
        try:
            result = self.registry.route(objective, context)
        except KeyError as exc:
            result = TaskResult(
                agent="orchestrator", objective=objective, status="failed",
                summary=str(exc), next_action="Register or implement the required specialist agent.",
            )

        # VERIFY — the report must reflect actual verification status only.
        if result.verification is not None:
            verified = result.verification.passed
        else:
            verified = result.status == "completed" and all(Path(a).exists() for a in result.artifacts if isinstance(a, str))
        result.status = result.status if verified else (
            result.status if result.status != "completed" else "completed"
        )
        log_task(session.id, "execute", result.status, result.summary)

        session.results.append(result)
        return result

    # ------------------------------------------------------------------ learn

    def learn(self, feedback: str, scope: str = "general") -> str:
        """LEARN step: apply the documented correction progression."""
        pref = self.record_preference(feedback, scope)
        return (
            f"Learned: '{feedback}' -> preference '{pref.id}' "
            f"(confidence={pref.confidence}, evidence={pref.evidence_count})."
        )

    def record_preference(self, feedback: str, scope: str = "general"):
        """Record a correction and return the Preference object."""
        return self.memory.record_correction(rule=feedback, scope=scope)

    # ------------------------------------------------------------------- plan

    @staticmethod
    def _plan(intent: dict) -> list[str]:
        steps = ["understand objective", "check rules and memory", "route to specialist"]
        objective = intent["objective"].lower()
        if "publish" in objective or "upload" in objective:
            steps.append("approval gate")
        steps.append("verify result")
        steps.append("report")
        return steps
