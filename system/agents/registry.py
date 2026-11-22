"""Specialist agent registry.

Maps role names from AGENT_SPEC section 5 to agent implementations and
routes objectives to the correct specialist.
"""

from __future__ import annotations

from system.agents.base import SpecialistAgent, TaskResult
from system.agents.creative.thumbnail_agent import ThumbnailAgent
from system.agents.editing.editing_agent import EditingAgent
from system.memory import MemoryStore

# role keyword -> agent factory. Order matters: first match wins.
# "edit" is checked before "creative" so "edit my video" routes correctly.
_ROUTING = [
    ("editing", lambda memory=None: EditingAgent(memory=memory)),
    ("edit plan", lambda memory=None: EditingAgent(memory=memory)),
    ("edit ", lambda memory=None: EditingAgent(memory=memory)),
    ("thumbnail", lambda memory=None: ThumbnailAgent(memory=memory)),
    ("creative", lambda memory=None: ThumbnailAgent(memory=memory)),
]


class AgentRegistry:
    def __init__(self, memory: MemoryStore | None = None) -> None:
        self._cache: dict[str, SpecialistAgent] = {}
        self._memory = memory

    def available(self) -> list[str]:
        return sorted({name for name, _ in _ROUTING})

    def get(self, role: str) -> SpecialistAgent:
        role_key = role.strip().lower()
        if role_key in self._cache:
            return self._cache[role_key]
        for keyword, factory in _ROUTING:
            if keyword in role_key:
                agent = factory(memory=self._memory)
                self._cache[role_key] = agent
                return agent
        raise KeyError(
            f"No specialist agent registered for role '{role}'. "
            f"Available: {', '.join(self.available())}"
        )

    def route(self, objective: str, context: dict | None = None) -> TaskResult:
        """Route an objective to the matching specialist agent."""
        agent = self.get(self.classify(objective, context))
        return agent.handle(objective, context)


    @staticmethod
    def classify(objective: str, context: dict | None = None) -> str:
        """Choose a specialist role from the objective text and context."""
        text = objective.lower()
        context = context or {}
        explicit = context.get("agent") or context.get("role")
        if explicit:
            return str(explicit)
        if "thumbnail" in text or "thumb" in text:
            return "creative"
        return "creative"


__all__ = ["AgentRegistry"]
