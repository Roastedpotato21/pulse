from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path


@dataclass(frozen=True)
class ExecutionTrace:
    id: int
    timestamp: str
    prompt: str
    error: str
    resolution: str


class EpisodicMemory:
    """Manages execution trace storage (Prompt, Error, Resolution) in workspace SQLite database."""

    def __init__(self, db_path: Path | None = None) -> None:
        self.db_path = db_path or Path(".agent/episodic-memory.sqlite3")
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS execution_traces (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    prompt TEXT NOT NULL,
                    error TEXT NOT NULL,
                    resolution TEXT NOT NULL
                )
                """
            )
            conn.commit()

    def log_trace(self, prompt: str, error: str, resolution: str) -> ExecutionTrace:
        timestamp = datetime.now(UTC).isoformat()
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO execution_traces (timestamp, prompt, error, resolution) VALUES (?, ?, ?, ?)",
                (prompt, error, resolution),
            )
            conn.commit()
            trace_id = cursor.lastrowid or 0

        return ExecutionTrace(
            id=trace_id,
            timestamp=timestamp,
            prompt=prompt,
            error=error,
            resolution=resolution,
        )

    def search_similar_resolutions(self, query: str, limit: int = 5) -> list[ExecutionTrace]:
        query_lower = f"%{query.lower()}%"
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT id, timestamp, prompt, error, resolution
                FROM execution_traces
                WHERE LOWER(prompt) LIKE ? OR LOWER(error) LIKE ? OR LOWER(resolution) LIKE ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (query_lower, query_lower, query_lower, limit),
            )
            rows = cursor.fetchall()

        return [
            ExecutionTrace(id=row[0], timestamp=row[1], prompt=row[2], error=row[3], resolution=row[4])
            for row in rows
        ]

    def get_all_traces(self) -> list[ExecutionTrace]:
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT id, timestamp, prompt, error, resolution FROM execution_traces ORDER BY id ASC")
            rows = cursor.fetchall()
        return [
            ExecutionTrace(id=row[0], timestamp=row[1], prompt=row[2], error=row[3], resolution=row[4])
            for row in rows
        ]
