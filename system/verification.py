"""Verification helpers.

AGENT_SPEC section 2: "The agent must not claim an action was completed
unless it actually verified completion."
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class CheckResult:
    name: str
    passed: bool
    detail: str = ""


@dataclass
class Verification:
    subject: str
    checks: list[CheckResult] = field(default_factory=list)

    def add(self, name: str, passed: bool, detail: str = "") -> CheckResult:
        result = CheckResult(name=name, passed=passed, detail=detail)
        self.checks.append(result)
        return result

    @property
    def passed(self) -> bool:
        return bool(self.checks) and all(c.passed for c in self.checks)

    def report(self) -> str:
        icon = lambda ok: "PASS" if ok else "FAIL"  # noqa: E731
        lines = [f"Verification of: {self.subject}"]
        for check in self.checks:
            detail = f" — {check.detail}" if check.detail else ""
            lines.append(f"  [{icon(check.passed)}] {check.name}{detail}")
        lines.append(f"Overall: {'VERIFIED' if self.passed else 'NOT VERIFIED'}")
        return "\n".join(lines)


def verify(condition: bool, subject: str, name: str, detail: str = "") -> Verification:
    """One-shot verify helper returning a single-check Verification."""
    v = Verification(subject=subject)
    v.add(name, bool(condition), detail)
    return v
