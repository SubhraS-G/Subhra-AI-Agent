"""Specialist agent base class.

Every specialist agent follows the core loop from AGENT_SPEC section 2:

    UNDERSTAND -> PLAN -> EXECUTE -> VERIFY -> LEARN
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from system.memory import MemoryStore
from system.permissions import PermissionGate
from system.verification import Verification


@dataclass
class TaskResult:
    """Standard result returned by every specialist agent."""

    agent: str
    objective: str
    status: str  # "completed" | "failed" | "needs_approval" | "needs_input"
    summary: str = ""
    artifacts: list = field(default_factory=list)
    verification: Verification | None = None
    next_action: str = ""

    def report(self) -> str:
        lines = [
            f"Agent:    {self.agent}",
            f"Objective: {self.objective}",
            f"Status:   {self.status}",
        ]
        if self.summary:
            lines.append(f"Summary:  {self.summary}")
        if self.artifacts:
            lines.append("Artifacts:")
            for artifact in self.artifacts:
                lines.append(f"  - {artifact}")
        if self.verification is not None:
            lines.append(self.verification.report())
        if self.next_action:
            lines.append(f"Next:     {self.next_action}")
        return "\n".join(lines)


class SpecialistAgent(ABC):
    """Base class for all specialist agents."""

    name: str = "specialist"
    role: str = ""

    def __init__(self, memory: MemoryStore | None = None, permissions: PermissionGate | None = None) -> None:
        self.memory = memory or MemoryStore()
        self.permissions = permissions or PermissionGate()

    @abstractmethod
    def handle(self, objective: str, context: dict | None = None) -> TaskResult:
        """Execute an objective and return a verified result."""
        raise NotImplementedError

    # ------------------------------------------------------- core loop hooks

    def understand(self, objective: str, context: dict | None = None) -> dict:
        """Parse the objective into structured intent. Override to specialize."""
        return {"objective": objective, "context": context or {}}

    def plan(self, intent: dict) -> list[str]:
        """Return ordered plan steps. Override to specialize."""
        return [f"Execute objective: {intent.get('objective', '')}"]

    def learn(self, feedback: str, scope: str = "general") -> None:
        """Record explicit feedback using the documented learning progression."""
        self.memory.record_correction(rule=feedback, scope=scope)
