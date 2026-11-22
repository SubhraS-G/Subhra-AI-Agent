"""Core documents loader.

The MD files in the repository root are the single source of truth. This
module loads them so behavior never has to be hard-coded twice.
"""

from __future__ import annotations

import functools
from dataclasses import dataclass, field

from system.paths import CORE_DOCS


@dataclass
class CoreDocs:
    """Parsed view of the core repository documents."""

    agent_spec: str = ""
    behavior_rules: str = ""
    permissions: str = ""
    user_profile: str = ""
    memory_doc: str = ""
    _raw: dict = field(default_factory=dict, repr=False)

    def section(self, doc: str, heading: str) -> str:
        """Return the body of `## heading` inside one of the core docs."""
        text = self._raw.get(doc, "")
        lines = text.splitlines()
        start = None
        for i, line in enumerate(lines):
            if line.strip().lower() == f"## {heading}".lower():
                start = i + 1
                break
        if start is None:
            return ""
        end = len(lines)
        for j in range(start, len(lines)):
            if lines[j].startswith("## "):
                end = j
                break
        return "\n".join(lines[start:end]).strip()


@functools.lru_cache(maxsize=1)
def load_core_docs() -> CoreDocs:
    """Load the five core documents from disk (cached for the process)."""
    docs = CoreDocs()
    mapping = {
        "agent_spec": "agent_spec",
        "behavior_rules": "behavior_rules",
        "permissions": "permissions",
        "user_profile": "user_profile",
        "memory_doc": "memory_doc",
    }
    for attr, key in mapping.items():
        path = CORE_DOCS[key]
        if path.exists():
            text = path.read_text(encoding="utf-8")
            setattr(docs, attr, text)
            docs._raw[attr] = text
    return docs
