from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
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
            timestamp=datetime.now(timezone.utc).isoformat(),
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

        print("\nFiles and actions this session:")
        for entry in self.entries:
            print(f"- {entry.action}: {entry.file} ({entry.detail})")
