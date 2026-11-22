"""Permissions gate.

Implements PERMISSIONS.md. Every consequential action is checked against the
documented policy before execution; actions above their tier raise
PermissionDenied instead of executing.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from system import core_docs


class Tier(Enum):
    AUTOMATIC = "automatic"
    APPROVAL = "approval_required"
    EXPLICIT = "explicit_confirmation"


class PermissionDenied(Exception):
    """Raised when an action's required tier is above what is granted."""


@dataclass
class Action:
    name: str
    tier: Tier
    description: str = ""


# Registry derived from PERMISSIONS.md. Keep in sync with that document.
ACTION_REGISTRY: dict[str, Action] = {
    # Automatic
    "research": Action("research", Tier.AUTOMATIC, "Research"),
    "analysis": Action("analysis", Tier.AUTOMATIC, "Analysis"),
    "drafting": Action("drafting", Tier.AUTOMATIC, "Drafting"),
    "temp_files": Action("temp_files", Tier.AUTOMATIC, "Creating temporary files"),
    "thumbnail_concepts": Action("thumbnail_concepts", Tier.AUTOMATIC, "Generating thumbnail concepts"),
    "image_editing": Action("image_editing", Tier.AUTOMATIC, "Image editing"),
    "run_tests": Action("run_tests", Tier.AUTOMATIC, "Running tests"),
    "reports": Action("reports", Tier.AUTOMATIC, "Preparing reports"),
    "read_files": Action("read_files", Tier.AUTOMATIC, "Reading authorized project files"),
    # Approval required
    "publish_content": Action("publish_content", Tier.APPROVAL, "Publishing content"),
    "send_external_message": Action("send_external_message", Tier.APPROVAL, "Sending external messages"),
    "send_email": Action("send_email", Tier.APPROVAL, "Sending emails"),
    "push_deploy": Action("push_deploy", Tier.APPROVAL, "Pushing production deployments"),
    "public_repo_changes": Action("public_repo_changes", Tier.APPROVAL, "Making public repository changes"),
    "upload_public": Action("upload_public", Tier.APPROVAL, "Uploading public content"),
    "git_push": Action("git_push", Tier.APPROVAL, "git push"),
    "youtube_upload": Action("youtube_upload", Tier.APPROVAL, "YouTube upload"),
    "youtube_publish": Action("youtube_publish", Tier.APPROVAL, "YouTube publish"),
    "file_delete": Action("file_delete", Tier.APPROVAL, "Deleting files"),
    "email_read": Action("email_read", Tier.APPROVAL, "Reading email"),
    # Explicit confirmation
    "spend_money": Action("spend_money", Tier.EXPLICIT, "Spending money"),
    "delete_important": Action("delete_important", Tier.EXPLICIT, "Deleting important files"),
    "irreversible": Action("irreversible", Tier.EXPLICIT, "Irreversible destructive operations"),
    "account_change": Action("account_change", Tier.EXPLICIT, "Permanent account changes"),
    "high_impact_external": Action("high_impact_external", Tier.EXPLICIT, "High-impact external actions"),
}

TIER_ORDER = {Tier.AUTOMATIC: 0, Tier.APPROVAL: 1, Tier.EXPLICIT: 2}


class PermissionGate:
    """Checks intended actions against the documented policy."""

    def __init__(self, granted: Tier = Tier.AUTOMATIC) -> None:
        self.granted = granted
        self.doc = core_docs.load_core_docs()

    def check(self, action_name: str, requested: Tier = Tier.AUTOMATIC) -> bool:
        """Return True when the action is allowed at the requested tier.

        An action is allowed when its registry tier is within the granted
        tier AND the requested tier is within the granted tier.
        """
        action = ACTION_REGISTRY.get(action_name)
        if action is None:
            # Unknown actions are treated as approval-required: fail closed.
            return TIER_ORDER[self.granted] >= TIER_ORDER[Tier.APPROVAL] and TIER_ORDER[requested] <= TIER_ORDER[self.granted]
        required = max(action.tier, requested, key=lambda t: TIER_ORDER[t])
        return TIER_ORDER[required] <= TIER_ORDER[self.granted]

    def require(self, action_name: str, requested: Tier = Tier.AUTOMATIC) -> None:
        if not self.check(action_name, requested):
            action = ACTION_REGISTRY.get(action_name)
            required_tier = action.tier.value if action else "unknown"
            raise PermissionDenied(
                f"Action '{action_name}' requires tier '{required_tier}' "
                f"which exceeds granted tier '{self.granted.value}'. "
                "Ask the user for approval and retry with granted permissions."
            )

    def describe(self) -> str:
        lines = [f"Granted tier: {self.granted.value}", ""]
        lines.append("Automatic:")
        for action in ACTION_REGISTRY.values():
            if action.tier == Tier.AUTOMATIC:
                lines.append(f"  - {action.name}: {action.description}")
        lines.append("")
        lines.append("Approval required:")
        for action in ACTION_REGISTRY.values():
            if action.tier == Tier.APPROVAL:
                lines.append(f"  - {action.name}: {action.description}")
        lines.append("")
        lines.append("Explicit confirmation:")
        for action in ACTION_REGISTRY.values():
            if action.tier == Tier.EXPLICIT:
                lines.append(f"  - {action.name}: {action.description}")
        return "\n".join(lines)
