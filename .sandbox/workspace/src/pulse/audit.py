from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path


@dataclass(frozen=True)
class AuditEntry:
    timestamp: str
    action: str
    file: str
    detail: str


class AuditLog:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.entries: list[AuditEntry] = []

    def record(self, action: str, file: str, detail: str) -> None:
        entry = AuditEntry(
            timestamp=datetime.now(UTC).isoformat(),
            action=action,
            file=file,
            detail=detail,
        )
        self.entries.append(entry)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(asdict(entry), separators=(",", ":")) + "\n")

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
