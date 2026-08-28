"""Production-grade Session Manager for Pulse.

Provides session state management, allowing conversational context, 
active tasks, and workspace metadata to be persisted and recovered 
across CLI and VS Code restarts.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)
SESSION_SCHEMA_VERSION = 1


# ---------------------------------------------------------------------------
# Enums & Data Models
# ---------------------------------------------------------------------------


class SessionStatus(Enum):
    """Current state of a managed session."""

    ACTIVE = "ACTIVE"
    ARCHIVED = "ARCHIVED"
    ERROR = "ERROR"


@dataclass(slots=True)
class SessionTurn:
    """A single turn in the session's conversation."""
    
    role: str
    content: str
    timestamp: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class SessionEvent:
    """Structured event emitted for VS Code UI and RPC clients."""

    event_type: str
    session_id: str
    status: SessionStatus
    timestamp: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass
class Session:
    """A persistent unit of conversational and workspace state."""

    id: str
    title: str = "New Session"
    status: SessionStatus = SessionStatus.ACTIVE
    conversation: list[SessionTurn] = field(default_factory=list)
    active_tasks: list[str] = field(default_factory=list)
    checkpoints: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    def to_dict(self) -> dict[str, Any]:
        """Serialize Session object to JSON-compatible dictionary."""
        return {
            "schema_version": SESSION_SCHEMA_VERSION,
            "id": self.id,
            "title": self.title,
            "status": self.status.value,
            "conversation": [asdict(t) for t in self.conversation],
            "active_tasks": self.active_tasks,
            "checkpoints": self.checkpoints,
            "metadata": self.metadata,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Session:
        """Deserialize Session object from dictionary."""
        version = int(data.get("schema_version", 0))
        if version > SESSION_SCHEMA_VERSION:
            raise ValueError(
                f"Session schema v{version} is newer than supported v{SESSION_SCHEMA_VERSION}."
            )
        status = SessionStatus(data.get("status", "ACTIVE"))
        conversation = [
            SessionTurn(**t) for t in data.get("conversation", []) if isinstance(t, dict)
        ]
        
        return cls(
            id=data["id"],
            title=data.get("title", "New Session"),
            status=status,
            conversation=conversation,
            active_tasks=list(data.get("active_tasks", [])),
            checkpoints=list(data.get("checkpoints", [])),
            metadata=data.get("metadata", {}),
            created_at=data.get("created_at", datetime.now(UTC).isoformat()),
            updated_at=data.get("updated_at", datetime.now(UTC).isoformat()),
        )


# ---------------------------------------------------------------------------
# Session Store & Event Bus
# ---------------------------------------------------------------------------


class SessionStore:
    """File-backed persistence store for Sessions."""

    def __init__(self, workspace: Path | None = None) -> None:
        self.workspace = workspace or Path.cwd()
        self.store_dir = self.workspace / ".pulse" / "sessions"
        self.active_session_file = self.store_dir / "active_session.txt"
        self.store_dir.mkdir(parents=True, exist_ok=True)

    def _get_file_path(self, session_id: str) -> Path:
        return self.store_dir / f"{session_id}.json"

    def save(self, session: Session) -> None:
        """Persist a session to its JSON file."""
        temporary_path: Path | None = None
        try:
            file_path = self._get_file_path(session.id)
            temporary_path = file_path.with_name(f".{file_path.name}.{uuid.uuid4().hex}.tmp")
            with temporary_path.open("w", encoding="utf-8") as f:
                json.dump(session.to_dict(), f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temporary_path, file_path)
        except OSError as err:
            logger.error(f"Failed to persist session {session.id}: {err}")
        finally:
            if temporary_path:
                temporary_path.unlink(missing_ok=True)

    def load(self, session_id: str) -> Session | None:
        """Load a session from its JSON file."""
        file_path = self._get_file_path(session_id)
        if not file_path.exists():
            return None
        try:
            with file_path.open("r", encoding="utf-8") as f:
                data = json.load(f)
            version = int(data.get("schema_version", 0))
            session = Session.from_dict(data)
            if version < SESSION_SCHEMA_VERSION:
                backup = file_path.with_suffix(f".json.schema-v{version}.bak")
                if not backup.exists():
                    shutil.copy2(file_path, backup)
                self.save(session)
            return session
        except (OSError, ValueError, json.JSONDecodeError) as err:
            logger.error(f"Failed to load session {session_id}: {err}")
            return None

    def list_all(self) -> list[Session]:
        """List all available sessions."""
        sessions = []
        for file_path in self.store_dir.glob("*.json"):
            session = self.load(file_path.stem)
            if session:
                sessions.append(session)
        return sorted(sessions, key=lambda s: s.updated_at, reverse=True)

    def set_active_session_id(self, session_id: str) -> None:
        """Mark a session as the currently active one."""
        try:
            self.active_session_file.write_text(session_id, encoding="utf-8")
        except OSError as err:
            logger.error(f"Failed to write active session: {err}")

    def get_active_session_id(self) -> str | None:
        """Read the currently active session ID."""
        if not self.active_session_file.exists():
            return None
        try:
            return self.active_session_file.read_text(encoding="utf-8").strip()
        except OSError as err:
            logger.error(f"Failed to read active session: {err}")
            return None


SessionEventListener = Callable[[SessionEvent], Awaitable[None] | None]


class SessionEventBus:
    """Async event bus emitting structured session events."""

    def __init__(self) -> None:
        self._listeners: list[SessionEventListener] = []

    def subscribe(self, listener: SessionEventListener) -> None:
        if listener not in self._listeners:
            self._listeners.append(listener)

    def unsubscribe(self, listener: SessionEventListener) -> None:
        if listener in self._listeners:
            self._listeners.remove(listener)

    async def emit(self, event: SessionEvent) -> None:
        for listener in list(self._listeners):
            try:
                res = listener(event)
                if asyncio.iscoroutine(res) or hasattr(res, "__await__"):
                    await res
            # Intentionally broad to isolate execution boundaries and prevent crashes.
            except Exception as err:  # noqa: BLE001
                # Intentionally broad to isolate event listener failures from crashing the session manager.
                logger.warning(f"Error in session event listener: {err}")


# ---------------------------------------------------------------------------
# Session Manager
# ---------------------------------------------------------------------------


class SessionManager:
    """Principal Session Manager for Pulse.

    Handles session creation, resuming, archiving, event emissions,
    and coordinates state with the TaskManager.
    """

    def __init__(
        self,
        workspace: Path | None = None,
        task_manager: Any | None = None,
        telemetry: Any | None = None,
    ) -> None:
        self.workspace = workspace or Path.cwd()
        self.store = SessionStore(self.workspace)
        self.event_bus = SessionEventBus()
        self.task_manager = task_manager
        self.telemetry = telemetry
        self._active_session: Session | None = None

    @property
    def active_session(self) -> Session | None:
        """Get the currently loaded active session."""
        return self._active_session

    def _mark_updated(self, session: Session) -> None:
        session.updated_at = datetime.now(UTC).isoformat()
        self.store.save(session)

    async def create_session(self, title: str | None = None, make_active: bool = True) -> Session:
        """Create and optionally activate a new session."""
        session_id = str(uuid.uuid4())
        session = Session(
            id=session_id,
            title=title or "New Session",
            status=SessionStatus.ACTIVE
        )
        self.store.save(session)
        
        if make_active:
            await self._set_active_session(session)

        if self.telemetry and hasattr(self.telemetry, "log_event"):
            self.telemetry.log_event("session_created", session_id=session_id)

        await self.event_bus.emit(
            SessionEvent("session_created", session_id, session.status, payload={"title": session.title})
        )
        return session

    async def load_session(self, session_id: str) -> Session:
        """Load an existing session from store."""
        session = self.store.load(session_id)
        if not session:
            raise ValueError(f"Session {session_id} not found.")
        return session

    async def resume_session(self, session_id: str) -> Session:
        """Load a session and mark it as active."""
        session = await self.load_session(session_id)
        if session.status == SessionStatus.ARCHIVED:
            session.status = SessionStatus.ACTIVE
            self._mark_updated(session)

        await self._set_active_session(session)

        if self.telemetry and hasattr(self.telemetry, "log_event"):
            self.telemetry.log_event("session_resumed", session_id=session_id)

        await self.event_bus.emit(
            SessionEvent("session_resumed", session_id, session.status, payload={"title": session.title})
        )
        return session

    async def archive_session(self, session_id: str) -> Session:
        """Archive a session."""
        session = await self.load_session(session_id)
        session.status = SessionStatus.ARCHIVED
        self._mark_updated(session)

        if self._active_session and self._active_session.id == session_id:
            self._active_session = None
            try:
                self.store.active_session_file.unlink(missing_ok=True)
            except OSError:
                pass

        if self.telemetry and hasattr(self.telemetry, "log_event"):
            self.telemetry.log_event("session_archived", session_id=session_id)

        await self.event_bus.emit(
            SessionEvent("session_archived", session_id, session.status)
        )
        return session

    async def get_or_create_active_session(self) -> Session:
        """Retrieve the last active session, or create a new one if none exists."""
        if self._active_session:
            return self._active_session

        active_id = self.store.get_active_session_id()
        if active_id:
            try:
                session = await self.load_session(active_id)
                if session.status == SessionStatus.ACTIVE:
                    self._active_session = session
                    return session
            except ValueError:
                pass  # Session file might have been deleted

        # Fallback to creating a new one
        return await self.create_session(make_active=True)

    async def add_conversation_turn(self, role: str, content: str, session_id: str | None = None, metadata: dict[str, Any] | None = None) -> None:
        """Append a conversational turn to a session."""
        if session_id:
            session = await self.load_session(session_id)
        else:
            session = await self.get_or_create_active_session()

        turn = SessionTurn(role=role, content=content, metadata=metadata or {})
        session.conversation.append(turn)
        self._mark_updated(session)
        
        # If this is the active session, update in-memory reference
        if self._active_session and self._active_session.id == session.id:
            self._active_session = session

        await self.event_bus.emit(
            SessionEvent("session_turn_added", session.id, session.status, payload={"role": role, "length": len(content)})
        )

    async def _set_active_session(self, session: Session) -> None:
        self._active_session = session
        self.store.set_active_session_id(session.id)
