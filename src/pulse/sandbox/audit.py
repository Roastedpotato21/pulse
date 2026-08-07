"""Structured JSON Lines audit logging system.

Records timestamped audit entries with automatic secret redaction, policy decisions,
exit codes, duration, and container execution telemetry.

Security hardening:
    - JSON values sanitized against control character injection.
    - isolation_level field added to track execution security tier.
    - Log writes are atomic (single write per entry).
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pulse.sandbox.secrets import SecretScrubber

# Pattern to match control characters that could corrupt JSONL format.
# Includes \n (\x0a) and \r (\x0d) which are the primary log injection vectors.
# Preserves \t (\x09) as it's harmless in JSON (encoded as \t by json.dumps).
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0a-\x0c\x0e-\x1f]")


def _sanitize_log_value(value: str) -> str:
    """Remove control characters that could corrupt JSONL log format.

    Security rationale:
        An attacker could inject \\n followed by a crafted JSON object into
        a log target string, causing log injection (CWE-117). Stripping
        control characters prevents injected newlines from splitting entries.
    """
    return _CONTROL_CHARS.sub("", value)


@dataclass(frozen=True, slots=True)
class StructuredAuditEntry:
    """Telemetry audit entry recorded in JSON Lines format."""

    timestamp: str
    action: str
    target: str
    decision: str
    exit_code: int | None = None
    duration_ms: float | None = None
    container_id: str | None = None
    isolation_level: str = "container"  # "container", "host_unsafe", "unavailable"
    redacted: bool = False
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class StructuredAuditLogger:
    """Thread-safe JSON Lines audit logger with automatic secret scrubbing."""

    def __init__(
        self,
        log_path: Path,
        scrubber: SecretScrubber | None = None,
    ) -> None:
        self.log_path = log_path
        self.scrubber = scrubber or SecretScrubber()
        self._entries: list[StructuredAuditEntry] = []

    def record(
        self,
        action: str,
        target: str = "",
        decision: str = "allow",
        *,
        exit_code: int | None = None,
        duration_ms: float | None = None,
        container_id: str | None = None,
        isolation_level: str = "container",
        detail: str = "",
    ) -> StructuredAuditEntry:
        """Create, sanitize, and record an audit entry."""
        raw_detail = detail
        raw_target = target

        clean_detail = self.scrubber.redact(raw_detail)
        clean_target = self.scrubber.redact(raw_target)
        was_redacted = (clean_detail != raw_detail) or (clean_target != raw_target)

        # Sanitize against log injection (CWE-117)
        clean_detail = _sanitize_log_value(clean_detail)
        clean_target = _sanitize_log_value(clean_target)
        clean_action = _sanitize_log_value(action)

        entry = StructuredAuditEntry(
            timestamp=datetime.now(UTC).isoformat(),
            action=clean_action,
            target=clean_target,
            decision=decision.lower(),
            exit_code=exit_code,
            duration_ms=duration_ms,
            container_id=container_id,
            isolation_level=isolation_level,
            redacted=was_redacted,
            detail=clean_detail,
        )

        self._entries.append(entry)
        self._write_entry(entry)
        return entry

    def _write_entry(self, entry: StructuredAuditEntry) -> None:
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.log_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry.to_dict(), separators=(",", ":")) + "\n")
        except OSError:
            pass  # Fallback gracefully if disk access fails

    @property
    def entries(self) -> list[StructuredAuditEntry]:
        return list(self._entries)

    def last_entry(self) -> StructuredAuditEntry | None:
        return self._entries[-1] if self._entries else None
