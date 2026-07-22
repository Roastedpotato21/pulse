from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


@dataclass
class MetricEvent:
    timestamp: str
    event_type: str
    step: Optional[int]
    duration_ms: Optional[float]
    metadata: dict[str, Any]


class TelemetryLogger:
    """Lightweight structured logger for agent step telemetry and execution metrics."""

    def __init__(self, log_path: Optional[Path] = None) -> None:
        self.log_path = log_path

    def log_event(
        self,
        event_type: str,
        step: Optional[int] = None,
        duration_ms: Optional[float] = None,
        **metadata: Any,
    ) -> MetricEvent:
        event = MetricEvent(
            timestamp=datetime.now(timezone.utc).isoformat(),
            event_type=event_type,
            step=step,
            duration_ms=duration_ms,
            metadata=metadata,
        )

        if self.log_path:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.log_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(asdict(event), separators=(",", ":")) + "\n")

        return event

    def log_step_execution(
        self, step: int, tool_name: Optional[str], duration_ms: float, success: bool, **kwargs: Any
    ) -> MetricEvent:
        return self.log_event(
            event_type="step_execution",
            step=step,
            duration_ms=duration_ms,
            tool_name=tool_name,
            success=success,
            **kwargs,
        )
