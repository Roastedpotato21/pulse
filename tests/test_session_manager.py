import asyncio
import json
from pathlib import Path

import pytest

from pulse.session_manager import (
    SessionEvent,
    SessionManager,
    SessionStatus,
)


@pytest.fixture
def workspace_dir(tmp_path: Path) -> Path:
    return tmp_path


@pytest.fixture
def session_manager(workspace_dir: Path) -> SessionManager:
    return SessionManager(workspace=workspace_dir)


def test_session_creation(session_manager: SessionManager) -> None:
    # Use asyncio.run since create_session is async
    session = asyncio.run(session_manager.create_session("Test Session"))

    assert session.title == "Test Session"
    assert session.status == SessionStatus.ACTIVE
    assert session.id is not None
    assert len(session.conversation) == 0

    # Ensure it was persisted
    store_file = session_manager.store.store_dir / f"{session.id}.json"
    assert store_file.exists()

    # Ensure it's active
    assert session_manager.active_session is not None
    assert session_manager.active_session.id == session.id
    active_id = session_manager.store.get_active_session_id()
    assert active_id == session.id


def test_add_conversation_turn(session_manager: SessionManager) -> None:
    session = asyncio.run(session_manager.create_session("Chat Session"))
    
    asyncio.run(session_manager.add_conversation_turn("user", "Hello"))
    asyncio.run(session_manager.add_conversation_turn("agent", "Hi there"))

    assert len(session_manager.active_session.conversation) == 2
    assert session_manager.active_session.conversation[0].role == "user"
    assert session_manager.active_session.conversation[1].content == "Hi there"

    # Reload from disk to verify persistence
    loaded = asyncio.run(session_manager.load_session(session.id))
    assert len(loaded.conversation) == 2


def test_session_archiving(session_manager: SessionManager) -> None:
    session = asyncio.run(session_manager.create_session("To Archive"))
    assert session_manager.active_session.id == session.id

    archived = asyncio.run(session_manager.archive_session(session.id))
    assert archived.status == SessionStatus.ARCHIVED
    assert session_manager.active_session is None
    assert session_manager.store.get_active_session_id() is None


def test_session_resume(session_manager: SessionManager) -> None:
    session = asyncio.run(session_manager.create_session("To Resume"))
    asyncio.run(session_manager.archive_session(session.id))

    resumed = asyncio.run(session_manager.resume_session(session.id))
    assert resumed.status == SessionStatus.ACTIVE
    assert session_manager.active_session.id == resumed.id
    assert session_manager.store.get_active_session_id() == resumed.id


def test_auto_recovery_on_startup(workspace_dir: Path) -> None:
    manager1 = SessionManager(workspace=workspace_dir)
    session = asyncio.run(manager1.create_session("Persistent Session"))
    asyncio.run(manager1.add_conversation_turn("user", "Data"))

    # Simulate restart
    manager2 = SessionManager(workspace=workspace_dir)
    recovered = asyncio.run(manager2.get_or_create_active_session())
    
    assert recovered.id == session.id
    assert recovered.title == "Persistent Session"
    assert len(recovered.conversation) == 1
    assert manager2.active_session.id == session.id


def test_session_event_bus_emission(session_manager: SessionManager) -> None:
    events_received: list[SessionEvent] = []

    async def listener(event: SessionEvent) -> None:
        events_received.append(event)

    session_manager.event_bus.subscribe(listener)

    session = asyncio.run(session_manager.create_session("Event Session"))
    asyncio.run(session_manager.add_conversation_turn("user", "Test event"))
    asyncio.run(session_manager.archive_session(session.id))

    event_types = [e.event_type for e in events_received]
    assert "session_created" in event_types
    assert "session_turn_added" in event_types
    assert "session_archived" in event_types


def test_legacy_session_is_backed_up_and_migrated_atomically(
    session_manager: SessionManager,
) -> None:
    path = session_manager.store.store_dir / "legacy.json"
    path.write_text(
        json.dumps({"id": "legacy", "title": "Legacy", "status": "ACTIVE"}),
        encoding="utf-8",
    )

    session = session_manager.store.load("legacy")

    assert session is not None and session.title == "Legacy"
    assert path.with_suffix(".json.schema-v0.bak").is_file()
    assert json.loads(path.read_text(encoding="utf-8"))["schema_version"] == 1
