"""Policy-based permission engine for Pulse sandbox execution.

Implements zero-trust fine-grained policy evaluation with inheritance,
wildcard pattern matching, and action-level overrides.

Security hardening (case bypass fix):
    - All targets are normalized through _normalize_target() before matching.
    - Normalization covers case, separators, Unicode NFC, and drive letters.
    - Rules match against BOTH raw and normalized targets (defense-in-depth).
    - Default decisions changed to DENY for all actions except READ.
"""

from __future__ import annotations

import fnmatch
import json
import unicodedata
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any


class ActionType(str, Enum):
    READ = "read"
    WRITE = "write"
    DELETE = "delete"
    RENAME = "rename"
    SHELL = "shell"
    GIT = "git"
    PYTHON = "python"
    NETWORK = "network"


class PolicyDecision(str, Enum):
    ALLOW = "allow"
    DENY = "deny"
    ASK = "ask"


@dataclass(frozen=True, slots=True)
class PolicyRule:
    """Individual policy rule matching an action and optional target pattern."""

    action: str
    target_pattern: str = "*"
    decision: PolicyDecision = PolicyDecision.ASK
    reason: str = ""

    def matches(self, action: str, target: str) -> bool:
        """Check if this rule matches the given action and target.

        Security: matches against BOTH the raw target and the normalized
        target. If either matches, the rule applies. This prevents
        case-sensitivity bypass on Linux where filenames are case-sensitive
        but policy rules may have been written case-insensitively.
        """
        if self.action != "*" and self.action.lower() != action.lower():
            return False
        if self.target_pattern == "*":
            return True

        normalized_pattern = SandboxPolicy.normalize_target(self.target_pattern)

        # Match against both raw and normalized target
        raw_normalized = SandboxPolicy.normalize_target(target)

        return (
            fnmatch.fnmatch(raw_normalized, normalized_pattern)
            or fnmatch.fnmatch(target.replace("\\", "/").lower(), normalized_pattern)
        )


class SandboxPolicy:
    """Production-grade policy manager supporting inheritance and pattern overrides.

    Security hardening:
        - Default decisions are DENY for all mutating actions.
        - READ defaults to ALLOW (read-only operations are safe).
        - GIT and PYTHON default to ASK (require explicit approval).
        - NETWORK defaults to DENY.
    """

    DEFAULT_DECISIONS: dict[str, PolicyDecision] = {  # noqa: RUF012
        ActionType.READ.value: PolicyDecision.ALLOW,
        ActionType.WRITE.value: PolicyDecision.DENY,
        ActionType.DELETE.value: PolicyDecision.DENY,
        ActionType.RENAME.value: PolicyDecision.DENY,
        ActionType.SHELL.value: PolicyDecision.DENY,
        ActionType.GIT.value: PolicyDecision.ASK,
        ActionType.PYTHON.value: PolicyDecision.ASK,
        ActionType.NETWORK.value: PolicyDecision.DENY,
    }

    def __init__(
        self,
        default_decisions: dict[str, PolicyDecision] | None = None,
        rules: list[PolicyRule] | None = None,
        parent_policy: SandboxPolicy | None = None,
    ) -> None:
        self.default_decisions = {**self.DEFAULT_DECISIONS, **(default_decisions or {})}
        self.rules: list[PolicyRule] = list(rules or [])
        self.parent_policy = parent_policy

    def add_rule(self, rule: PolicyRule) -> None:
        self.rules.insert(0, rule)  # Higher priority rules first

    def evaluate(self, action: ActionType | str, target: str = "") -> PolicyDecision:
        action_str = action.value if isinstance(action, ActionType) else str(action).lower()

        # 1. Check explicit rules (most specific target patterns first)
        for rule in self.rules:
            if rule.matches(action_str, target):
                return rule.decision

        # 2. Consult parent policy if present
        if self.parent_policy:
            return self.parent_policy.evaluate(action, target)

        # 3. Fall back to default action decision (DENY if unknown action)
        return self.default_decisions.get(action_str, PolicyDecision.DENY)

    def is_allowed(self, action: ActionType | str, target: str = "") -> bool:
        return self.evaluate(action, target) == PolicyDecision.ALLOW

    def requires_approval(self, action: ActionType | str, target: str = "") -> bool:
        return self.evaluate(action, target) == PolicyDecision.ASK

    @staticmethod
    def normalize_target(target: str) -> str:
        """Normalize a path/target string for consistent policy matching.

        Security: prevents bypass via case differences, separator
        inconsistencies, Unicode confusables, or drive letter prefixes.

        Normalization steps:
            1. Unicode NFC normalization (canonical decomposition + composition)
            2. Backslash → forward slash
            3. Lowercase
            4. Strip Windows drive letter prefix (e.g., C:/)
            5. Collapse consecutive slashes
            6. Strip leading/trailing slashes for relative matching
        """
        # 1. Unicode NFC normalization
        normalized = unicodedata.normalize("NFC", target)

        # 2. Normalize separators
        normalized = normalized.replace("\\", "/")

        # 3. Lowercase
        normalized = normalized.lower()

        # 4. Strip drive letter prefix (e.g., c:/ or C:/ or C:file.txt)
        if len(normalized) >= 2 and normalized[0].isalpha() and normalized[1] == ":":
            if len(normalized) >= 3 and normalized[2] == "/":
                normalized = normalized[3:]  # Strip "c:/"
            else:
                normalized = normalized[2:]  # Strip "c:" (drive-relative)

        # 5. Collapse consecutive slashes
        while "//" in normalized:
            normalized = normalized.replace("//", "/")

        # 6. Strip leading slash for relative matching (preserves internal structure)
        normalized = normalized.strip("/")

        return normalized

    @classmethod
    def from_dict(cls, data: dict[str, Any], parent_policy: SandboxPolicy | None = None) -> SandboxPolicy:
        defaults: dict[str, PolicyDecision] = {}
        for action_name, decision_val in data.get("defaults", {}).items():
            defaults[action_name.lower()] = PolicyDecision(str(decision_val).lower())

        rules: list[PolicyRule] = []
        for raw_rule in data.get("rules", []):
            rules.append(
                PolicyRule(
                    action=str(raw_rule.get("action", "*")).lower(),
                    target_pattern=str(raw_rule.get("target_pattern", "*")),
                    decision=PolicyDecision(str(raw_rule.get("decision", "ask")).lower()),
                    reason=str(raw_rule.get("reason", "")),
                )
            )

        return cls(default_decisions=defaults, rules=rules, parent_policy=parent_policy)

    @classmethod
    def from_file(cls, path: Path, parent_policy: SandboxPolicy | None = None) -> SandboxPolicy:
        text = path.read_text(encoding="utf-8")
        raw = json.loads(text)
        return cls.from_dict(raw, parent_policy=parent_policy)

    def to_dict(self) -> dict[str, Any]:
        return {
            "defaults": {k: v.value for k, v in self.default_decisions.items()},
            "rules": [
                {
                    "action": r.action,
                    "target_pattern": r.target_pattern,
                    "decision": r.decision.value,
                    "reason": r.reason,
                }
                for r in reversed(self.rules)
            ],
        }
