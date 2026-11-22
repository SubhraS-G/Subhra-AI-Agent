"""Filesystem paths for the SURA agent system.

All paths are resolved relative to this file's parent directory (the agent
system root), so commands can be run from any working directory.
"""

from __future__ import annotations

from pathlib import Path

AGENT_ROOT: Path = Path(__file__).resolve().parent.parent

# The agent's personal name — used by the UI, CLI banner, and system prompt.
# Change these two lines to rebrand the whole system.
AGENT_NAME: str = "SURA"
AGENT_FULL_NAME: str = "SURA — Subhra's Unified Reasoning Assistant"

# Core documents (single source of truth on disk)
CORE_DOCS = {
    "agent_spec": AGENT_ROOT / "AGENT_SPEC.md",
    "behavior_rules": AGENT_ROOT / "BEHAVIOR_RULES.md",
    "permissions": AGENT_ROOT / "PERMISSIONS.md",
    "user_profile": AGENT_ROOT / "USER_PROFILE.md",
    "memory_doc": AGENT_ROOT / "MEMORY.md",
}

# Memory store
MEMORY_DIR = AGENT_ROOT / "memory"
PERMANENT_MEMORY = MEMORY_DIR / "permanent"
PROJECT_MEMORY = MEMORY_DIR / "projects"
WORKFLOW_MEMORY = MEMORY_DIR / "workflows"
TEMPORARY_MEMORY = MEMORY_DIR / "temporary"

# Logs
LOGS_DIR = AGENT_ROOT / "logs"

# Thumbnail system
THUMBNAIL_SYSTEM = AGENT_ROOT / "thumbnail-system"
FACE_LIBRARY = AGENT_ROOT / "face-library"
FACE_METADATA = FACE_LIBRARY / "metadata.yaml"

# Video-editing specialty (style profiles + edit plans)
EDITING_DIR = AGENT_ROOT / "editing"
EDITING_PLANS_DIR = EDITING_DIR / "plans"

# Brands
BRANDS_DIR = AGENT_ROOT / "brands"


def ensure_directories() -> None:
    """Create every runtime directory the system needs (idempotent)."""
    for directory in (
        PERMANENT_MEMORY,
        PROJECT_MEMORY,
        WORKFLOW_MEMORY,
        TEMPORARY_MEMORY,
        LOGS_DIR,
        THUMBNAIL_SYSTEM / "references" / "liked",
        THUMBNAIL_SYSTEM / "references" / "disliked",
        THUMBNAIL_SYSTEM / "output",
        THUMBNAIL_SYSTEM / "templates",
        FACE_LIBRARY,
        EDITING_PLANS_DIR,
    ):
        directory.mkdir(parents=True, exist_ok=True)
