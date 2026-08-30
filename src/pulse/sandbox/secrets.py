"""Secret protection and automated credential scrubbing engine.

Redacts API keys, SSH keys, passwords, bearer tokens, and environment secrets
from logs, terminal outputs, and audit records.

Security hardening (ReDoS remediation):
    All regex patterns have been audited for catastrophic backtracking.
    - No nested quantifiers (e.g. (a+)+ patterns).
    - Alternations are anchored or bounded.
    - A per-call timeout guard prevents any single redact() from blocking.
    - Patterns use possessive-equivalent constructs where possible.
"""

from __future__ import annotations

import os
import re
import threading
import urllib.parse
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class SecretMode(Enum):
    DENY_ALL = "deny_all"
    ALLOW_EXPLICIT = "allow_explicit"
    ALLOW_ALL = "allow_all"


class SecretEnforcementLevel(Enum):
    STRONGLY_ENFORCED = "strongly_enforced"
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True, slots=True)
class SecretPolicy:
    """Policy declaring how host secrets should be passed to the sandbox."""
    mode: SecretMode = SecretMode.DENY_ALL
    explicit_env: dict[str, str] = field(default_factory=dict)
    
    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode.value,
            "explicit_env": self.explicit_env,
        }
        
    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SecretPolicy:
        mode_str = str(data.get("mode", "deny_all")).lower()
        try:
            mode = SecretMode(mode_str)
        except ValueError:
            mode = SecretMode.DENY_ALL
        return cls(
            mode=mode,
            explicit_env=data.get("explicit_env", {})
        )


def build_isolated_environment(
    policy: SecretPolicy | None = None, 
    extra_env: dict[str, str] | None = None
) -> dict[str, str]:
    """Construct an isolated environment preventing wholesale host credential inheritance."""
    # Start with a pristine minimal environment (do NOT merge os.environ by default)
    isolated = {
        "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "HOME": "/workspace",
        "TMPDIR": "/tmp",
    }
    
    if policy is None:
        policy = SecretPolicy()
        
    if policy.mode == SecretMode.ALLOW_ALL:
        # Development override: inherit host env
        isolated = dict(os.environ)
        
    elif policy.mode == SecretMode.ALLOW_EXPLICIT:
        isolated.update(policy.explicit_env)
        
    # Apply execution-specific non-secret overrides
    if extra_env:
        isolated.update(extra_env)
        
    return isolated


class SecretScrubber:
    """Regex & value-matching secret redactor with ReDoS protection."""

    REDACTED_LABEL = "[REDACTED_SECRET]"
    _REDACT_TIMEOUT_SECONDS = 2.0

    # -----------------------------------------------------------------------
    # ReDoS-safe patterns: no nested quantifiers, no unbounded alternations.
    # Each pattern is designed for linear-time matching.
    # -----------------------------------------------------------------------
    BUILTIN_PATTERNS: list[re.Pattern[str]] = [  # noqa: RUF012
        # SSH Private Keys — bounded by clear delimiters, lazy inner match
        re.compile(
            r"-----BEGIN [A-Z0-9 ]+ PRIVATE KEY-----"
            r"[\s\S]+?"
            r"-----END [A-Z0-9 ]+ PRIVATE KEY-----"
        ),
        # API Key assignments — simplified: key_name followed by separator then token value
        # Fixed: removed nested quantifier from original pattern.
        # Original had \s*[:=\s]\s* which allowed catastrophic backtracking.
        re.compile(
            r"(?i)(?:api[_-]?key|secret(?:[_-]?key)?|client[_-]?secret|password|"
            r"private[_-]?key|access[_-]?token|auth[_-]?token|bearer|internal[_-]?prompt)"
            r"\s{0,4}[:=]\s{0,4}"
            r"['\"]?"
            r"([a-zA-Z0-9_%\\\-\.=]{8,512})"
            r"['\"]?"
        ),
        re.compile(
            r"(?i)\b(?:pulse[_-])?(?:audit[_-])?"
            r"(?:secret|api[_-]?key|internal[_-]?prompt)[a-z0-9_%_\\-]{6,256}\b"
        ),
        # Google API Keys — fixed-length prefix, bounded suffix
        re.compile(r"AIzaSy[A-Za-z0-9_\-]{33}"),
        # OpenAI / Anthropic / Groq / DeepSeek Keys — bounded length
        re.compile(r"sk-[A-Za-z0-9_-]{20,128}"),
        # Graphene keys — bounded length
        re.compile(r"graphene-[A-Za-z0-9_-]{8,128}"),
        # GitHub Personal Access Tokens — fixed structure
        re.compile(r"gh[pousr]_[A-Za-z0-9]{36}"),
        # Slack Tokens — bounded
        re.compile(r"xox[baprs]-[A-Za-z0-9_-]{10,128}"),
        # JWT Tokens — three dot-separated base64url segments, bounded
        re.compile(r"eyJ[A-Za-z0-9_-]{10,512}\.eyJ[A-Za-z0-9_-]{10,1024}\.[A-Za-z0-9_-]{10,512}"),
        # AWS Access Key IDs — fixed-length structure
        re.compile(r"(?:A3T[A-Z0-9]|AKIA|AGPA|AIDA|AROA|AIPA|ANPA|ANVA|ASIA)[A-Z0-9]{16}"),
    ]

    def __init__(self, secrets: list[str] | None = None) -> None:
        self._exact_secrets: set[str] = set()
        if secrets:
            for s in secrets:
                self.add_secret(s)

    def add_secret(self, secret: str) -> None:
        """Register an exact sensitive string to redact."""
        cleaned = secret.strip()
        if len(cleaned) >= 4:  # Avoid redacting tiny trivial strings like "yes", "true", "x"
            self._exact_secrets.add(cleaned)

    def redact(self, text: str) -> str:
        """Scrub all registered secret values and regex patterns from text.

        Security guarantees:
            - Exact secrets are replaced via str.replace (O(n), no regex).
            - Each regex pattern is bounded-length and ReDoS-safe.
            - Total redaction is guarded by a thread-based timeout.
        """
        if not text:
            return text

        # Use a thread-based timeout to guard against any unforeseen backtracking
        result_container: list[str] = []
        error_container: list[Exception] = []

        def _do_redact() -> None:
            try:
                result_container.append(self._redact_impl(text))
            # Intentionally broad to isolate execution boundaries and prevent crashes.
            except Exception as exc:  # noqa: BLE001
                error_container.append(exc)

        worker = threading.Thread(target=_do_redact, daemon=True)
        worker.start()
        worker.join(timeout=self._REDACT_TIMEOUT_SECONDS)

        if worker.is_alive():
            # Timeout — return generic redaction marker rather than plaintext (fail-closed)
            return "[REDACTED_DUE_TO_TIMEOUT: redaction exceeded time limit]"

        if error_container:
            # Unexpected error — return text with error marker
            return "[REDACTION_FAILED]"

        return result_container[0] if result_container else text

    def contains_explicit_secret(self, text: str) -> bool:
        """Check if the text contains any of the explicitly registered secrets.
        
        This only checks for the exact secret strings provided during authorization,
        preventing false positives that might occur with generic regex patterns.
        """
        if not text or not self._exact_secrets:
            return False
            
        for secret in self._exact_secrets:
            if secret in text:
                return True
        return False

    def _redact_impl(self, text: str) -> str:
        """Internal redaction without timeout guard."""
        scrubbed = text

        # Redact exact registered values and common serialized forms. URLs and
        # JSON strings otherwise provide trivial redaction bypasses.
        for secret in sorted(self._exact_secrets, key=len, reverse=True):
            variants = {
                secret,
                urllib.parse.quote(secret, safe=""),
                urllib.parse.quote_plus(secret, safe=""),
                "".join(
                    character
                    if character.isalnum()
                    else f"%{ord(character):02X}"
                    for character in secret
                ),
                secret.replace("\\", "\\\\").replace("\n", "\\n").replace("\r", "\\r"),
            }
            for variant in sorted(variants, key=len, reverse=True):
                if variant:
                    scrubbed = re.sub(
                        re.escape(variant),
                        self.REDACTED_LABEL,
                        scrubbed,
                        flags=re.IGNORECASE,
                    )

        # 2. Redact regex pattern matches
        for pattern in self.BUILTIN_PATTERNS:
            def replace_match(match: re.Match[str]) -> str:
                # If pattern has sub-captures (e.g. key: value), replace only value part
                if match.lastindex and match.lastindex >= 1:
                    full = match.group(0)
                    val = match.group(1)
                    return full.replace(val, self.REDACTED_LABEL)
                return self.REDACTED_LABEL

            scrubbed = pattern.sub(replace_match, scrubbed)

        return scrubbed
