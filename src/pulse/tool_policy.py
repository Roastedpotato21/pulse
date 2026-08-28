"""Typed contracts and centralized authorization for model tool calls.

Tool invocations are untrusted model output.  This module keeps validation and
authorization ahead of execution, with an explicit decision that can be
audited without recording sensitive argument values.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Protocol


class ToolRisk(str, Enum):
    """Impact level used to decide whether human approval is required."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class AuthorizationDecision(str, Enum):
    """The only authorization outcomes available to the registry."""

    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"


class ArgumentKind(str, Enum):
    """Supported wire-level argument types for a tool schema."""

    BOOLEAN = "boolean"
    INTEGER = "integer"
    NUMBER = "number"
    STRING = "string"
    OBJECT = "object"
    ARRAY = "array"
    CALLABLE = "callable"


@dataclass(frozen=True, slots=True)
class ToolArgument:
    """A named, typed argument accepted by one tool."""

    name: str
    kind: ArgumentKind
    required: bool = False


@dataclass(frozen=True, slots=True)
class ToolSchema:
    """Strict schema for untrusted tool arguments.

    Unknown arguments are rejected by default.  A tool may opt out only where
    it intentionally implements a command-style sub-protocol itself.
    """

    arguments: tuple[ToolArgument, ...] = ()
    allow_extra: bool = False

    def validate(self, arguments: Mapping[str, Any]) -> str | None:
        declared = {argument.name: argument for argument in self.arguments}
        for argument in self.arguments:
            if argument.required and argument.name not in arguments:
                return f"Missing required argument '{argument.name}'."

        if not self.allow_extra:
            extras = sorted(set(arguments) - set(declared))
            if extras:
                return f"Unexpected argument(s): {', '.join(extras)}."

        for name, value in arguments.items():
            argument = declared.get(name)
            if argument and not _matches_kind(value, argument.kind):
                return f"Argument '{name}' must be a {argument.kind.value}."
        return None


def _matches_kind(value: Any, kind: ArgumentKind) -> bool:
    if kind == ArgumentKind.BOOLEAN:
        return isinstance(value, bool)
    if kind == ArgumentKind.INTEGER:
        return isinstance(value, int) and not isinstance(value, bool)
    if kind == ArgumentKind.NUMBER:
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if kind == ArgumentKind.STRING:
        return isinstance(value, str)
    if kind == ArgumentKind.OBJECT:
        return isinstance(value, Mapping)
    if kind == ArgumentKind.ARRAY:
        return isinstance(value, list)
    return callable(value)


@dataclass(frozen=True, slots=True)
class ToolAuthorization:
    """An inspectable authorization result, safe to put in an audit log."""

    decision: AuthorizationDecision
    reason: str
    subject_id: str
    capability: str
    risk: ToolRisk


class AuditRecorder(Protocol):
    def record(self, action: str, file: str, detail: str) -> None: ...


@dataclass(slots=True)
class ToolPolicyEngine:
    """Evaluate a capability allowlist, workspace scope, and approval policy."""

    workspace: Path
    subject_id: str = "local-user"
    allowed_capabilities: frozenset[str] = field(default_factory=frozenset)
    approval_risks: frozenset[ToolRisk] = field(
        default_factory=lambda: frozenset({ToolRisk.MEDIUM, ToolRisk.HIGH})
    )
    audit_log: AuditRecorder | None = None

    def authorize(
        self,
        *,
        tool_name: str,
        capability: str,
        risk: ToolRisk,
        arguments: Mapping[str, Any],
    ) -> ToolAuthorization:
        """Return a deterministic decision before an executor sees arguments."""
        if capability not in self.allowed_capabilities:
            return self._decision(
                AuthorizationDecision.DENY,
                f"Capability '{capability}' is not allowlisted for this runtime.",
                capability,
                risk,
            )

        target_error = self._validate_workspace_scope(arguments)
        if target_error:
            return self._decision(AuthorizationDecision.DENY, target_error, capability, risk)

        if risk in self.approval_risks:
            return self._decision(
                AuthorizationDecision.ASK,
                f"{risk.value.capitalize()}-risk capability '{tool_name}' requires approval.",
                capability,
                risk,
            )
        return self._decision(
            AuthorizationDecision.ALLOW,
            f"Capability '{tool_name}' is allowlisted for {self.subject_id}.",
            capability,
            risk,
        )

    def record(self, authorization: ToolAuthorization) -> None:
        """Record a decision without persisting untrusted tool argument values."""
        if self.audit_log:
            self.audit_log.record(
                f"tool-policy-{authorization.decision.value}",
                authorization.capability,
                (
                    f"subject={authorization.subject_id} risk={authorization.risk.value}; "
                    f"{authorization.reason}"
                ),
            )

    def _decision(
        self,
        decision: AuthorizationDecision,
        reason: str,
        capability: str,
        risk: ToolRisk,
    ) -> ToolAuthorization:
        return ToolAuthorization(decision, reason, self.subject_id, capability, risk)

    def _validate_workspace_scope(self, arguments: Mapping[str, Any]) -> str | None:
        """Reject paths outside the configured workspace before execution."""
        root = self.workspace.resolve()
        for key in ("file", "path", "source", "destination", "cwd", "working_directory"):
            value = arguments.get(key)
            if value is None:
                continue
            if not isinstance(value, str):
                return f"Workspace argument '{key}' must be a string path."
            candidate = Path(value)
            resolved = candidate.resolve() if candidate.is_absolute() else (root / candidate).resolve()
            try:
                resolved.relative_to(root)
            except ValueError:
                return f"Workspace argument '{key}' escapes the authorized workspace."
        return None
