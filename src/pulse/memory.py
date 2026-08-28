"""SQLite-backed long-term project memory, independent of Pulse interfaces."""

from __future__ import annotations

import asyncio
import json
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from pulse.storage import migrate_database

MEMORY_SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class MemoryEntry:
    id: int
    category: str
    content: str
    tags: tuple[str, ...]
    created_at: str


class RequestWithMessage(Protocol):
    message: str


class LongTermMemory:
    """Small async façade over a workspace-local SQLite memory database."""

    def __init__(self, workspace: Path, database_path: Path | None = None) -> None:
        self.workspace = workspace.resolve()
        self.database_path = database_path or self.workspace / ".agent" / "pulse-memory.sqlite3"
        self._lock = asyncio.Lock()
        migrate_database(self.database_path, MEMORY_SCHEMA_VERSION, self._migrate_schema)

    @staticmethod
    def _migrate_schema(connection: sqlite3.Connection, _current: int) -> None:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS memories (id INTEGER PRIMARY KEY, category TEXT NOT NULL, content TEXT NOT NULL, tags TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
        )
        connection.execute(
            "CREATE TABLE IF NOT EXISTS preferences (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
        )

    async def store_project_context(self, content: str, *, tags: tuple[str, ...] = ()) -> MemoryEntry:
        return await self._store("project", content, tags)

    async def remember_agent_information(self, content: str, *, tags: tuple[str, ...] = ()) -> MemoryEntry:
        return await self._store("agent", content, tags)

    async def remember_task(self, request: str, response: str) -> MemoryEntry:
        tags = tuple(sorted(set(self._terms(request))))[:12]
        return await self._store("task", f"Request: {request}\nResponse: {response[:4_000]}", tags)

    async def record_workflow(
        self,
        request: str,
        tool_sequence: tuple[str, ...] | list[str],
        *,
        success: bool,
        error: str | None = None,
        summary: str | None = None,
    ) -> MemoryEntry:
        payload = {
            "request": request.strip(),
            "tool_sequence": [str(tool) for tool in tool_sequence],
            "success": bool(success),
            "error": str(error or ""),
            "summary": str(summary or ""),
        }
        terms = set(self._terms(request))
        terms.update(self._terms(" ".join(payload["tool_sequence"])))
        tags = tuple(sorted(terms))[:12]
        return await self._store("workflow", json.dumps(payload, sort_keys=True), tags)

    async def workflow_recommendations(self, query: str, *, limit: int = 4) -> list[dict[str, Any]]:
        async with self._lock:
            return await asyncio.to_thread(self._workflow_recommendations, query, limit)

    async def set_preference(self, key: str, value: str) -> None:
        if not key.strip() or not value.strip():
            raise ValueError("Preference key and value are required.")
        async with self._lock:
            await asyncio.to_thread(self._set_preference, key.strip(), value.strip())

    async def preferences(self) -> dict[str, str]:
        async with self._lock:
            return await asyncio.to_thread(self._preferences)

    async def retrieve(self, query: str, *, limit: int = 6) -> list[MemoryEntry]:
        async with self._lock:
            return await asyncio.to_thread(self._retrieve, query, limit)

    async def context_for(self, query: str, *, limit: int = 4) -> list[str]:
        """Format durable preferences and relevant memories as approved context."""
        preferences, entries = await asyncio.gather(self.preferences(), self.retrieve(query, limit=limit))
        context = [f"User preference — {key}: {value}" for key, value in preferences.items()]
        context.extend(f"Remembered {entry.category}: {entry.content}" for entry in entries)
        return context

    async def _store(self, category: str, content: str, tags: tuple[str, ...]) -> MemoryEntry:
        if not content.strip():
            raise ValueError("Memory content is required.")
        async with self._lock:
            return await asyncio.to_thread(self._store_sync, category, content.strip(), tags)

    def _connection(self) -> sqlite3.Connection:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.database_path)
        connection.execute("PRAGMA journal_mode=WAL")
        return connection

    def _store_sync(self, category: str, content: str, tags: tuple[str, ...]) -> MemoryEntry:
        with self._connection() as connection:
            cursor = connection.execute(
                "INSERT INTO memories(category, content, tags) VALUES (?, ?, ?)", (category, content, json.dumps(tags)),
            )
            row = connection.execute("SELECT id, category, content, tags, created_at FROM memories WHERE id = ?", (cursor.lastrowid,)).fetchone()
        return self._entry(row)

    def _set_preference(self, key: str, value: str) -> None:
        with self._connection() as connection:
            connection.execute(
                "INSERT INTO preferences(key, value, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP) ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = CURRENT_TIMESTAMP",
                (key, value),
            )

    def _preferences(self) -> dict[str, str]:
        with self._connection() as connection:
            rows = connection.execute("SELECT key, value FROM preferences ORDER BY key").fetchall()
        return {str(key): str(value) for key, value in rows}

    def _retrieve(self, query: str, limit: int) -> list[MemoryEntry]:
        terms = set(self._terms(query))
        with self._connection() as connection:
            rows = connection.execute("SELECT id, category, content, tags, created_at FROM memories ORDER BY id DESC").fetchall()
        entries = [self._entry(row) for row in rows]
        if not terms:
            return entries[:limit]
        ranked = [
            (len(terms.intersection(self._terms(entry.content + " " + " ".join(entry.tags)))), entry)
            for entry in entries
        ]
        return [entry for score, entry in sorted(ranked, key=lambda item: (-item[0], -item[1].id)) if score][:limit]

    def _workflow_recommendations(self, query: str, limit: int) -> list[dict[str, Any]]:
        terms = set(self._terms(query))
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT id, category, content, tags, created_at FROM memories WHERE category = 'workflow' ORDER BY id DESC"
            ).fetchall()
        workflow_entries = []
        for row in rows:
            entry = self._entry(row)
            try:
                payload = json.loads(entry.content)
            except json.JSONDecodeError:
                continue
            if not isinstance(payload, dict):
                continue
            workflow_entries.append({
                "id": entry.id,
                "request": str(payload.get("request", "")),
                "tool_sequence": list(payload.get("tool_sequence", [])),
                "success": bool(payload.get("success")),
                "error": str(payload.get("error", "")),
                "summary": str(payload.get("summary", "")),
                "created_at": entry.created_at,
            })
        if not terms:
            return workflow_entries[:limit]
        ranked = []
        for workflow in workflow_entries:
            score = len(terms.intersection(self._terms(workflow["request"] + " " + " ".join(workflow["tool_sequence"]))))
            ranked.append((score, workflow))
        sorted_workflows = [workflow for score, workflow in sorted(ranked, key=lambda item: (-item[0], -item[1]["id"])) if score]
        return sorted_workflows[:limit]

    @staticmethod
    def _entry(row: tuple[Any, ...]) -> MemoryEntry:
        return MemoryEntry(int(row[0]), str(row[1]), str(row[2]), tuple(json.loads(row[3])), str(row[4]))

    @staticmethod
    def _terms(value: str) -> list[str]:
        return re.findall(r"[a-zA-Z_][a-zA-Z0-9_]*", value.lower())


class MemoryContextSource:
    """Adapter for the core Agent context protocol without importing it."""

    def __init__(self, memory: LongTermMemory) -> None:
        self.memory = memory

    async def context_for(self, request: RequestWithMessage) -> list[str]:
        return await self.memory.context_for(request.message)
