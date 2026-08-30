"""SQLite-backed Conversation Manager for Pulse.

Supports multiple named conversations, turn storage, auto-titling,
full-text search, export (Markdown / JSON), and active-conversation tracking.

Database location: <workspace>/.agent/conversations.sqlite3
"""
from __future__ import annotations

import json
import re
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pulse.sandbox.secrets import SecretScrubber
from pulse.storage import migrate_database

CONVERSATION_SCHEMA_VERSION = 1

# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ConversationTurn:
    id: int
    conv_id: str
    role: str          # "user" | "assistant" | "system"
    content: str
    created_at: str


@dataclass(frozen=True, slots=True)
class Conversation:
    id: str
    title: str
    created_at: str
    updated_at: str
    turn_count: int = 0


# ---------------------------------------------------------------------------
# ConversationManager
# ---------------------------------------------------------------------------


class ConversationManager:
    """Thread-safe (single-threaded) façade over a local SQLite conversation store."""

    def __init__(self, workspace: Path, database_path: Path | None = None) -> None:
        self.workspace = workspace.resolve()
        self.database_path = (
            database_path or self.workspace / ".agent" / "conversations.sqlite3"
        )
        self._scrubber = SecretScrubber()
        self._ensure_schema()

    # ------------------------------------------------------------------
    # Schema bootstrap
    # ------------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.database_path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _ensure_schema(self) -> None:
        def migration(conn: sqlite3.Connection, _current: int) -> None:
            conn.execute("""CREATE TABLE IF NOT EXISTS conversations (
                    id         TEXT PRIMARY KEY,
                    title      TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )""")
            conn.execute("""CREATE TABLE IF NOT EXISTS turns (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    conv_id    TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
                    role       TEXT NOT NULL,
                    content    TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )""")
            conn.execute("""CREATE TABLE IF NOT EXISTS meta (
                    key   TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )""")
            conn.execute("CREATE INDEX IF NOT EXISTS turns_conv_idx ON turns(conv_id)")

        migrate_database(self.database_path, CONVERSATION_SCHEMA_VERSION, migration)

    # ------------------------------------------------------------------
    # Conversation CRUD
    # ------------------------------------------------------------------

    def create(self, title: str | None = None) -> Conversation:
        """Create a new conversation and set it as the active one."""
        conv_id = str(uuid.uuid4())
        now = datetime.now(UTC).isoformat()
        display_title = self._scrubber.redact(title or "New Conversation")
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO conversations(id, title, created_at, updated_at) VALUES (?, ?, ?, ?)",
                (conv_id, display_title, now, now),
            )
        self.switch(conv_id)
        return Conversation(id=conv_id, title=display_title, created_at=now, updated_at=now, turn_count=0)

    def auto_title(self, conv_id: str, first_message: str) -> Conversation:
        """Generate a tidy title from the first user message (≤ 60 chars)."""
        # Strip punctuation/whitespace, take first 60 chars, capitalize
        cleaned = re.sub(r"\s+", " ", self._scrubber.redact(first_message).strip())
        title = cleaned[:60].rstrip(" ,.:;!?")
        if len(cleaned) > 60:
            title += "…"
        return self.rename(conv_id, title or "Conversation")

    def get(self, conv_id: str) -> Conversation | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id, title, created_at, updated_at FROM conversations WHERE id = ?",
                (conv_id,),
            ).fetchone()
            if row is None:
                return None
            count = conn.execute(
                "SELECT COUNT(*) FROM turns WHERE conv_id = ?", (conv_id,)
            ).fetchone()[0]
        return Conversation(id=row[0], title=row[1], created_at=row[2], updated_at=row[3], turn_count=count)

    def list_all(self) -> list[Conversation]:
        """Return all conversations ordered by most recently updated."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT c.id, c.title, c.created_at, c.updated_at, COUNT(t.id) AS turn_count "
                "FROM conversations c LEFT JOIN turns t ON t.conv_id = c.id "
                "GROUP BY c.id ORDER BY c.updated_at DESC"
            ).fetchall()
        return [Conversation(id=r[0], title=r[1], created_at=r[2], updated_at=r[3], turn_count=r[4]) for r in rows]

    def rename(self, conv_id: str, new_title: str) -> Conversation:
        new_title = self._scrubber.redact(new_title).strip() or "Untitled"
        now = datetime.now(UTC).isoformat()
        with self._connect() as conn:
            conn.execute(
                "UPDATE conversations SET title = ?, updated_at = ? WHERE id = ?",
                (new_title, now, conv_id),
            )
        conv = self.get(conv_id)
        if conv is None:
            raise ValueError(f"Conversation {conv_id!r} not found.")
        return conv

    def delete(self, conv_id: str) -> None:
        """Delete a conversation and all its turns. Clears active if it was active."""
        with self._connect() as conn:
            conn.execute("DELETE FROM conversations WHERE id = ?", (conv_id,))
        # If active conversation was the deleted one, clear it
        if self._get_meta("active_conv_id") == conv_id:
            self._set_meta("active_conv_id", "")

    def switch(self, conv_id: str) -> Conversation:
        """Mark a conversation as the active one. Returns the conversation."""
        conv = self.get(conv_id)
        if conv is None:
            raise ValueError(f"Conversation {conv_id!r} not found.")
        self._set_meta("active_conv_id", conv_id)
        return conv

    def get_active(self) -> Conversation | None:
        """Return the currently active conversation, or None if unset/deleted."""
        conv_id = self._get_meta("active_conv_id")
        if not conv_id:
            return None
        return self.get(conv_id)

    # ------------------------------------------------------------------
    # Turn management
    # ------------------------------------------------------------------

    def add_turn(self, conv_id: str, role: str, content: str) -> ConversationTurn:
        content = self._scrubber.redact(content)
        now = datetime.now(UTC).isoformat()
        with self._connect() as conn:
            cursor = conn.execute(
                "INSERT INTO turns(conv_id, role, content, created_at) VALUES (?, ?, ?, ?)",
                (conv_id, role, content, now),
            )
            turn_id = cursor.lastrowid
            # Bump updated_at on the conversation
            conn.execute(
                "UPDATE conversations SET updated_at = ? WHERE id = ?", (now, conv_id)
            )
        return ConversationTurn(id=turn_id, conv_id=conv_id, role=role, content=content, created_at=now)

    def get_turns(self, conv_id: str) -> list[ConversationTurn]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, conv_id, role, content, created_at FROM turns WHERE conv_id = ? ORDER BY id ASC",
                (conv_id,),
            ).fetchall()
        return [ConversationTurn(id=r[0], conv_id=r[1], role=r[2], content=r[3], created_at=r[4]) for r in rows]

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def search(self, query: str) -> list[Conversation]:
        """Full-text LIKE search on conversation titles and turn content."""
        if not query.strip():
            return self.list_all()
        pattern = f"%{query.strip()}%"
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT DISTINCT c.id, c.title, c.created_at, c.updated_at,
                       (SELECT COUNT(*) FROM turns t2 WHERE t2.conv_id = c.id) AS turn_count
                FROM conversations c
                LEFT JOIN turns t ON t.conv_id = c.id
                WHERE c.title LIKE ? OR t.content LIKE ?
                ORDER BY c.updated_at DESC
                """,
                (pattern, pattern),
            ).fetchall()
        return [Conversation(id=r[0], title=r[1], created_at=r[2], updated_at=r[3], turn_count=r[4]) for r in rows]

    # ------------------------------------------------------------------
    # Export
    # ------------------------------------------------------------------

    def export(
        self,
        conv_id: str,
        output_path: Path | None = None,
        fmt: str = "md",
    ) -> Path:
        """Export a conversation to Markdown or JSON.

        Args:
            conv_id: The conversation UUID to export.
            output_path: Explicit file path, or auto-generated in workspace root.
            fmt: "md" (default) or "json".
        """
        conv = self.get(conv_id)
        if conv is None:
            raise ValueError(f"Conversation {conv_id!r} not found.")
        turns = self.get_turns(conv_id)

        ext = "md" if fmt == "md" else "json"
        if output_path is None:
            safe_title = re.sub(r"[^\w\-]", "_", conv.title)[:40]
            output_path = self.workspace / f"{safe_title}_{conv_id[:8]}.{ext}"
        else:
            output_path = Path(output_path)

        output_path.parent.mkdir(parents=True, exist_ok=True)

        if fmt == "json":
            data: dict[str, Any] = {
                "id": conv.id,
                "title": conv.title,
                "created_at": conv.created_at,
                "updated_at": conv.updated_at,
                "turns": [
                    {"role": t.role, "content": t.content, "timestamp": t.created_at}
                    for t in turns
                ],
            }
            output_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        else:
            lines: list[str] = [
                f"# {conv.title}",
                "",
                f"> **Created:** {conv.created_at}  ",
                f"> **Updated:** {conv.updated_at}",
                "",
                "---",
                "",
            ]
            for turn in turns:
                label = "**You**" if turn.role == "user" else "**Pulse**"
                lines.append(f"### {label}  ")
                lines.append(f"*{turn.created_at}*")
                lines.append("")
                lines.append(turn.content)
                lines.append("")
                lines.append("---")
                lines.append("")
            output_path.write_text("\n".join(lines), encoding="utf-8")

        return output_path

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_meta(self, key: str) -> str:
        with self._connect() as conn:
            row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return str(row[0]) if row else ""

    def _set_meta(self, key: str, value: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO meta(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
