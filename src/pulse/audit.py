from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from pulse.sandbox.secrets import SecretScrubber
from pulse.telemetry import get_correlation_id


@dataclass(frozen=True)
class AuditEntry:
    schema_version: int = field(default=1, init=False)
    timestamp: str
    correlation_id: str
    action: str
    file: str
    detail: str


class AuditLog:
    def __init__(self, path: Path, secrets: list[str] | None = None) -> None:
        self.path = path
        self.entries: list[AuditEntry] = []
        self._scrubber = SecretScrubber(secrets)

    def add_secret(self, secret: str | None) -> None:
        if secret:
            self._scrubber.add_secret(secret)

    def record(self, action: str, file: str, detail: str) -> None:
        entry = AuditEntry(
            timestamp=datetime.now(UTC).isoformat(),
            correlation_id=get_correlation_id(),
            action=self._scrubber.redact(action),
            file=self._scrubber.redact(file),
            detail=self._scrubber.redact(detail),
        )
        self.entries.append(entry)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(asdict(entry), separators=(",", ":")) + "\n")
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def print_summary(self) -> None:
        if not self.entries:
            return

        from rich.console import Console
        from rich.table import Table

        from pulse.cli_ui import _box_style

        console = Console()
        table = Table(box=_box_style(), show_header=True, title="Session Audit Log")
        table.add_column("Action", style="bold cyan")
        table.add_column("File", style="yellow")
        table.add_column("Detail", style="dim")

        for entry in self.entries:
            table.add_row(entry.action, entry.file, entry.detail)

        console.print()
        console.print(table)
        console.print()
