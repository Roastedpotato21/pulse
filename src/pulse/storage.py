"""Transactional SQLite schema migration, backup, and restore primitives."""

from __future__ import annotations

import argparse
import os
import sqlite3
import tempfile
from collections.abc import Callable
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

Migration = Callable[[sqlite3.Connection, int], None]


def schema_version(database_path: Path) -> int:
    if not database_path.exists():
        return 0
    with closing(sqlite3.connect(database_path)) as connection:
        return int(connection.execute("PRAGMA user_version").fetchone()[0])


def backup_database(source: Path, destination: Path) -> Path:
    source = source.resolve()
    destination = destination.resolve()
    if not source.is_file():
        raise FileNotFoundError(source)
    if source == destination:
        raise ValueError("Backup destination must differ from the source database.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with (
        closing(sqlite3.connect(source)) as source_connection,
        closing(sqlite3.connect(destination)) as backup_connection,
    ):
        source_connection.backup(backup_connection)
        result = backup_connection.execute("PRAGMA integrity_check").fetchone()
        if not result or result[0] != "ok":
            raise sqlite3.DatabaseError("Backup integrity check failed.")
    return destination


def restore_database(backup: Path, destination: Path) -> Path:
    """Restore a verified backup atomically; the destination must not be in use."""
    backup = backup.resolve()
    destination = destination.resolve()
    if not backup.is_file():
        raise FileNotFoundError(backup)
    destination.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.restore-", suffix=".sqlite3", dir=destination.parent
    )
    os.close(handle)
    temporary_path = Path(temporary_name)
    try:
        backup_database(backup, temporary_path)
        for suffix in ("-wal", "-shm"):
            Path(f"{destination}{suffix}").unlink(missing_ok=True)
        os.replace(temporary_path, destination)
    finally:
        temporary_path.unlink(missing_ok=True)
    return destination


def migrate_database(
    database_path: Path,
    target_version: int,
    migration: Migration,
    *,
    timeout: float = 10.0,
) -> Path | None:
    """Migrate one database transactionally and back up any existing schema."""
    if target_version < 1:
        raise ValueError("Target schema version must be positive.")
    database_path = database_path.resolve()
    database_path.parent.mkdir(parents=True, exist_ok=True)
    backup_path: Path | None = None
    with closing(
        sqlite3.connect(database_path, timeout=timeout, isolation_level=None)
    ) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        current = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if current > target_version:
            raise RuntimeError(
                f"Database schema v{current} is newer than supported v{target_version}."
            )
        if current == target_version:
            return None
        has_user_tables = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' LIMIT 1"
        ).fetchone()
        if has_user_tables:
            stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
            backup_path = (
                database_path.parent
                / "backups"
                / f"{database_path.stem}.schema-v{current}-to-v{target_version}.{stamp}.sqlite3"
            )
            backup_database(database_path, backup_path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            migration(connection, current)
            connection.execute(f"PRAGMA user_version = {target_version}")
            connection.execute("COMMIT")
        except BaseException:
            connection.execute("ROLLBACK")
            raise
    return backup_path


def main() -> int:
    parser = argparse.ArgumentParser(description="Inspect, back up, or restore Pulse SQLite state.")
    commands = parser.add_subparsers(dest="command", required=True)
    version_parser = commands.add_parser("version")
    version_parser.add_argument("database", type=Path)
    backup_parser = commands.add_parser("backup")
    backup_parser.add_argument("source", type=Path)
    backup_parser.add_argument("destination", type=Path)
    restore_parser = commands.add_parser("restore")
    restore_parser.add_argument("backup", type=Path)
    restore_parser.add_argument("destination", type=Path)
    restore_parser.add_argument("--confirm-stopped", action="store_true")
    args = parser.parse_args()
    try:
        if args.command == "version":
            print(schema_version(args.database))
        elif args.command == "backup":
            print(backup_database(args.source, args.destination))
        else:
            if not args.confirm_stopped:
                parser.error("restore requires --confirm-stopped")
            print(restore_database(args.backup, args.destination))
    except (OSError, RuntimeError, sqlite3.DatabaseError, ValueError) as exc:
        print(f"Storage operation failed: {exc}")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
