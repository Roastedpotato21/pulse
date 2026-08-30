from __future__ import annotations

import contextvars
import json
import re
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pulse.sandbox.secrets import SecretScrubber

_CORRELATION_ID: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "pulse_correlation_id", default=None
)
_SAFE_CORRELATION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


def set_correlation_id(value: object | None = None) -> str:
    candidate = str(value).strip() if value is not None else ""
    correlation_id = (
        candidate if _SAFE_CORRELATION_ID.fullmatch(candidate) else uuid.uuid4().hex
    )
    _CORRELATION_ID.set(correlation_id)
    return correlation_id


def get_correlation_id() -> str:
    return _CORRELATION_ID.get() or set_correlation_id()


@contextmanager
def correlation_scope(value: object | None = None):
    correlation_id = str(value).strip() if value is not None else ""
    if not _SAFE_CORRELATION_ID.fullmatch(correlation_id):
        correlation_id = uuid.uuid4().hex
    token = _CORRELATION_ID.set(correlation_id)
    try:
        yield correlation_id
    finally:
        _CORRELATION_ID.reset(token)


@dataclass
class MetricEvent:
    schema_version: int = field(default=1, init=False)
    timestamp: str
    correlation_id: str
    event_type: str
    step: int | None
    duration_ms: float | None
    metadata: dict[str, Any]


class TelemetryLogger:
    """Lightweight structured logger for agent step telemetry and execution metrics."""

    def __init__(self, log_path: Path | None = None) -> None:
        self.log_path = log_path
        self._scrubber = SecretScrubber()

    def add_secret(self, secret: str | None) -> None:
        if secret:
            self._scrubber.add_secret(secret)

    def _sanitize(self, value: Any) -> Any:
        if isinstance(value, str):
            return self._scrubber.redact(value)
        if isinstance(value, dict):
            return {str(key): self._sanitize(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [self._sanitize(item) for item in value]
        return value

    def log_event(
        self,
        event_type: str,
        step: int | None = None,
        duration_ms: float | None = None,
        **metadata: Any,
    ) -> MetricEvent:
        event = MetricEvent(
            timestamp=datetime.now(UTC).isoformat(),
            correlation_id=get_correlation_id(),
            event_type=event_type,
            step=step,
            duration_ms=duration_ms,
            metadata=self._sanitize(metadata),
        )

        if self.log_path:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.log_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(asdict(event), separators=(",", ":")) + "\n")

        return event

    def log_step_execution(
        self, step: int, tool_name: str | None, duration_ms: float, success: bool, **kwargs: Any
    ) -> MetricEvent:
        return self.log_event(
            event_type="step_execution",
            step=step,
            duration_ms=duration_ms,
            tool_name=tool_name,
            success=success,
            **kwargs,
        )
