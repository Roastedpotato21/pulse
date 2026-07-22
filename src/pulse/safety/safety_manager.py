from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from enum import Enum
from pathlib import Path

from pulse.audit import AuditLog


class RiskLevel(Enum):
    LOW = "LOW"        # Read-only operations
    MEDIUM = "MEDIUM"  # File edits and test executions
    HIGH = "HIGH"      # System execution, shell commands, or file deletion


class SafetyManager:
    """Assesses action risk levels, enforces user confirmation for HIGH risk actions, and records audit logs."""

    def __init__(
        self,
        audit_log: AuditLog | None = None,
        confirmation_callback: Callable[[str, RiskLevel], bool | Awaitable[bool]] | None = None,
    ) -> None:
        self.audit_log = audit_log
        self.confirmation_callback = confirmation_callback

    def assess_risk(self, action: str, target: str = "") -> RiskLevel:
        action_lower = action.lower()
        target_lower = target.lower()

        # HIGH risk: system command execution, shell access, deletion
        high_keywords = {
            "execute",
            "system",
            "shell",
            "delete",
            "remove",
            "destroy",
            "drop",
            "unlink",
            "bash",
            "cmd",
            "terminal",
            "eval",
        }
        if any(kw in action_lower for kw in high_keywords) or "delete" in target_lower or "remove" in target_lower:
            return RiskLevel.HIGH

        # MEDIUM risk: edits, code modifications, writing files, running test suites
        medium_keywords = {
            "edit",
            "modify",
            "write",
            "update",
            "patch",
            "test",
            "pytest",
            "mutate",
            "create",
        }
        if any(kw in action_lower for kw in medium_keywords):
            return RiskLevel.MEDIUM

        return RiskLevel.LOW

    async def authorize(self, action: str, target: str = "", detail: str = "") -> bool:
        risk = self.assess_risk(action, target)
        authorized = True

        if risk is RiskLevel.HIGH:
            if self.confirmation_callback is not None:
                res = self.confirmation_callback(action, risk)
                if asyncio.iscoroutine(res) or hasattr(res, "__await__"):
                    authorized = bool(await res)
                else:
                    authorized = bool(res)
            else:
                authorized = False

        if self.audit_log:
            status = "APPROVED" if authorized else "REJECTED"
            self.audit_log.record(
                action=f"safety-{risk.value.lower()}-{status.lower()}",
                file=target or ".",
                detail=f"Action: {action} | Risk: {risk.value} | Authorized: {authorized} | {detail}".strip(" |"),
            )

        return authorized

    def log_audit(self, action: str, target: str, detail: str, risk: RiskLevel | None = None) -> None:
        if self.audit_log:
            risk_label = risk.value if risk else "UNKNOWN"
            self.audit_log.record(
                action=action,
                file=target or ".",
                detail=f"[{risk_label}] {detail}",
            )
