from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from pulse.conversations.manager import (
    CONVERSATION_SCHEMA_VERSION,
    ConversationManager,
)
from pulse.episodic import EPISODIC_SCHEMA_VERSION, EpisodicMemory
from pulse.memory import MEMORY_SCHEMA_VERSION, LongTermMemory
from pulse.sandbox.remote.server import (
    REMOTE_EXECUTION_SCHEMA_VERSION,
    RemoteExecutionStore,
)
from pulse.storage import (
    backup_database,
    migrate_database,
    restore_database,
    schema_version,
)
from pulse.task_manager import TASK_SCHEMA_VERSION, TaskStore


def test_migration_is_versioned_and_backs_up_existing_data(tmp_path: Path) -> None:
    database = tmp_path / "legacy.sqlite3"
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("CREATE TABLE records(value TEXT NOT NULL)")
        connection.execute("INSERT INTO records VALUES ('before')")
        connection.commit()

    def migration(connection: sqlite3.Connection, current: int) -> None:
        assert current == 0
        connection.execute("ALTER TABLE records ADD COLUMN migrated INTEGER DEFAULT 1")

    backup = migrate_database(database, 1, migration)

    assert backup is not None and backup.is_file()
    assert schema_version(database) == 1
    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute("SELECT value, migrated FROM records").fetchone() == (
            "before",
            1,
        )
    with closing(sqlite3.connect(backup)) as connection:
        assert connection.execute("SELECT value FROM records").fetchone() == ("before",)


def test_failed_migration_rolls_back_and_backup_restores_atomically(tmp_path: Path) -> None:
    database = tmp_path / "state.sqlite3"
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("CREATE TABLE records(value TEXT NOT NULL)")
        connection.execute("INSERT INTO records VALUES ('known-good')")
        connection.commit()
    backup = backup_database(database, tmp_path / "manual-backup.sqlite3")

    def failing_migration(connection: sqlite3.Connection, _current: int) -> None:
        connection.execute("UPDATE records SET value = 'partial'")
        raise RuntimeError("simulated migration crash")

    with pytest.raises(RuntimeError, match="simulated"):
        migrate_database(database, 1, failing_migration)
    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute("SELECT value FROM records").fetchone() == (
            "known-good",
        )
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("UPDATE records SET value = 'damaged'")
        connection.commit()
    restore_database(backup, database)
    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert connection.execute("SELECT value FROM records").fetchone() == (
            "known-good",
        )


def test_supported_sqlite_stores_publish_schema_versions(tmp_path: Path) -> None:
    memory_path = tmp_path / "memory.sqlite3"
    episodic_path = tmp_path / "episodic.sqlite3"
    conversations_path = tmp_path / "conversations.sqlite3"
    LongTermMemory(tmp_path, memory_path)
    EpisodicMemory(episodic_path)
    ConversationManager(tmp_path, conversations_path)
    tasks = TaskStore(tmp_path)
    remote_path = tmp_path / "remote.sqlite3"
    RemoteExecutionStore(remote_path)

    assert schema_version(memory_path) == MEMORY_SCHEMA_VERSION
    assert schema_version(episodic_path) == EPISODIC_SCHEMA_VERSION
    assert schema_version(conversations_path) == CONVERSATION_SCHEMA_VERSION
    assert schema_version(tasks.store_file) == TASK_SCHEMA_VERSION
    assert schema_version(remote_path) == REMOTE_EXECUTION_SCHEMA_VERSION


def test_remote_submission_replay_after_external_completion_is_rejected(
    tmp_path: Path,
) -> None:
    store = RemoteExecutionStore(tmp_path / "remote.sqlite3")
    store.create("execution-1", "tenant-1", "correlation-1")
    store.update("execution-1", "COMPLETED", {"exit_code": 0})
    with pytest.raises(sqlite3.IntegrityError):
        store.create("execution-1", "tenant-1", "correlation-replayed")

    record = store.get("execution-1", "tenant-1")
    assert record is not None
    assert record["status"] == "COMPLETED"
    assert record["correlation_id"] == "correlation-1"
