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

import re
import threading


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
            r"(?i)(?:api[_-]?key|secret[_-]?key|access[_-]?token|auth[_-]?token|bearer)"
            r"\s{0,4}[:=]\s{0,4}"
            r"['\"]?"
            r"([a-zA-Z0-9_\-\.=]{16,128})"
            r"['\"]?"
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
            # Timeout — return text with warning marker rather than blocking forever
            return text + "\n[SCRUB_TIMEOUT: redaction exceeded time limit]"

        if error_container:
            # Unexpected error — return text with error marker
            return text + f"\n[SCRUB_ERROR: {error_container[0]}]"

        return result_container[0] if result_container else text

    def _redact_impl(self, text: str) -> str:
        """Internal redaction without timeout guard."""
        scrubbed = text

        # 1. Redact exact registered secret values (longest first to avoid partial matches)
        for secret in sorted(self._exact_secrets, key=len, reverse=True):
            scrubbed = scrubbed.replace(secret, self.REDACTED_LABEL)

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
