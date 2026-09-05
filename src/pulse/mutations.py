from __future__ import annotations

import hashlib
import json
import os
import subprocess
import uuid
from collections.abc import Iterator
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from difflib import unified_diff
from pathlib import Path
from typing import Self

from pulse.subprocesses import isolated_process_kwargs, isolated_subprocess_environment
from pulse.telemetry import get_correlation_id


@dataclass(frozen=True)
class FileSnapshot:
    path: str
    content: bytes
    sha256: str


@dataclass(frozen=True)
class MutationEvent:
    """An immutable, rollback-ready record of one workspace file mutation."""

    schema_version: int = field(default=1, init=False)
    transaction_id: str
    correlation_id: str
    timestamp: str
    action: str
    file_path: str
    before_content: str | None
    after_content: str | None
    before_sha256: str | None
    after_sha256: str | None
    unified_diff: str
    command: str | None
    generated_by_command: bool
    git_before: dict[str, object]
    git_after: dict[str, object]


class MutationTransaction:
    def __init__(self, tracker: MutationTracker, *, command: str | None = None) -> None:
        self.tracker = tracker
        self.id = str(uuid.uuid4())
        self.command = command
        self._before = tracker._snapshot_workspace()
        self._git_before = tracker.git_state()
        self._closed = False

    def finalize(self) -> list[MutationEvent]:
        if self._closed:
            return []
        self._closed = True
        after = self.tracker._snapshot_workspace()
        git_after = self.tracker.git_state()
        events = self.tracker._events_for_change(
            self.id, self._before, after, self._git_before, git_after, self.command
        )
        self.tracker._append(events)
        return events

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type, exc, traceback) -> bool:
        self.finalize()
        return False


class MutationTracker:
    """Capture file-system and Git changes made by Pulse operations.

    Content snapshots are intentional: they provide the data required for a future
    rollback implementation.  Consumers should protect `.agent/logs` because it may
    contain the previous or replacement contents of edited files.
    """

    _IGNORED_PARTS = {  # noqa: RUF012
        ".git",
        ".agent",
        ".agents",
        ".pulse",
        ".venv",
        "venv",
        "__pycache__",
        ".pytest_cache",
    }
    _IGNORED_NAMES = {".env", "credentials.json"}  # noqa: RUF012
    _IGNORED_SUFFIXES = {  # noqa: RUF012
        ".crt", ".db", ".key", ".log", ".p12", ".pem", ".pfx", ".sqlite", ".sqlite3"
    }

    def __init__(self, workspace: Path, log_path: Path | None = None) -> None:
        self.workspace = workspace.resolve()
        self.log_path = log_path or self.workspace / ".agent" / "logs" / "mutations.jsonl"

    def transaction(self, *, command: str | None = None) -> MutationTransaction:
        return MutationTransaction(self, command=command)

    def run_command(self, command: str, *, timeout: float | None = None) -> subprocess.CompletedProcess[str]:
        """Run a workspace command and record every file it creates or changes."""
        with self.transaction(command=command):
            return subprocess.run(
                command,
                cwd=self.workspace,
                shell=True,
                text=True,
                capture_output=True,
                timeout=timeout,
                check=False,
                env=isolated_subprocess_environment(),
                **isolated_process_kwargs(),
            )

    def git_state(self) -> dict[str, object]:
        return {
            "available": self._git("rev-parse", "--is-inside-work-tree") == "true",
            "commit": self._git("rev-parse", "HEAD"),
            "status": self._git("status", "--porcelain=v1"),
            "changed_files": self._git_lines("status", "--porcelain=v1"),
            "diff": self._git("diff", "--binary"),
        }

    def history(self) -> Iterator[dict[str, object]]:
        if not self.log_path.exists():
            return iter(())

        def entries() -> Iterator[dict[str, object]]:
            for line in self.log_path.read_text(encoding="utf-8").splitlines():
                try:
                    value = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(value, dict):
                    yield value

        return entries()

    def latest_transaction(self) -> list[dict[str, object]]:
        events = list(self.history())
        if not events:
            return []
        last_id = str(events[-1].get("transaction_id", ""))
        return [event for event in events if event.get("transaction_id") == last_id]

    def last_approved_edit(self) -> list[dict[str, object]]:
        """Return the latest approved edit batch, excluding rollback logs."""
        events = [
            event
            for event in self.history()
            if str(event.get("command", "")).startswith("pulse approved edit")
        ]
        if not events:
            return []
        command = str(events[-1].get("command", ""))
        if ":" in command:
            return [event for event in events if event.get("command") == command]
        transaction_id = events[-1].get("transaction_id")
        return [event for event in events if event.get("transaction_id") == transaction_id]

    def _snapshot_workspace(self) -> dict[str, FileSnapshot]:
        snapshots: dict[str, FileSnapshot] = {}
        for path in self.workspace.rglob("*"):
            relative = path.relative_to(self.workspace)
            if (
                any(part in self._IGNORED_PARTS for part in relative.parts)
                or path.name.lower() in self._IGNORED_NAMES
                or path.suffix.lower() in self._IGNORED_SUFFIXES
                or path.is_symlink()
                or not path.is_file()
            ):
                continue
            content = path.read_bytes()
            display_path = relative.as_posix()
            snapshots[display_path] = FileSnapshot(display_path, content, self._hash(content))
        return snapshots

    def _events_for_change(
        self,
        transaction_id: str,
        before: dict[str, FileSnapshot],
        after: dict[str, FileSnapshot],
        git_before: dict[str, object],
        git_after: dict[str, object],
        command: str | None,
    ) -> list[MutationEvent]:
        events: list[MutationEvent] = []
        paths = sorted(set(before) | set(after))
        for path in paths:
            previous, current = before.get(path), after.get(path)
            if previous and current and previous.sha256 == current.sha256:
                continue
            action = "create" if previous is None else "delete" if current is None else "modify"
            events.append(self._event(transaction_id, action, path, previous, current, command, git_before, git_after))

        # Detect a rename when a deleted file's exact content appears at a new path.
        deleted = [event for event in events if event.action == "delete"]
        created = [event for event in events if event.action == "create"]
        replacements: dict[str, MutationEvent] = {}
        removed: set[str] = set()
        for old in deleted:
            for new in created:
                if old.before_sha256 == new.after_sha256:
                    replacements[old.file_path] = MutationEvent(
                        transaction_id=transaction_id,
                        correlation_id=old.correlation_id,
                        timestamp=old.timestamp,
                        action="rename",
                        file_path=f"{old.file_path} -> {new.file_path}",
                        before_content=old.before_content,
                        after_content=new.after_content,
                        before_sha256=old.before_sha256,
                        after_sha256=new.after_sha256,
                        unified_diff="",
                        command=command,
                        generated_by_command=command is not None,
                        git_before=git_before,
                        git_after=git_after,
                    )
                    removed.add(new.file_path)
                    break
        return [event for event in events if event.file_path not in replacements and event.file_path not in removed] + list(replacements.values())

    def _event(self, transaction_id: str, action: str, path: str, before: FileSnapshot | None, after: FileSnapshot | None, command: str | None, git_before: dict[str, object], git_after: dict[str, object]) -> MutationEvent:
        before_text = self._decode(before.content) if before else None
        after_text = self._decode(after.content) if after else None
        return MutationEvent(
            transaction_id=transaction_id,
            correlation_id=get_correlation_id(),
            timestamp=datetime.now(UTC).isoformat(),
            action=action,
            file_path=path,
            before_content=before_text,
            after_content=after_text,
            before_sha256=before.sha256 if before else None,
            after_sha256=after.sha256 if after else None,
            unified_diff=self._diff(path, before_text, after_text),
            command=command,
            generated_by_command=command is not None,
            git_before=git_before,
            git_after=git_after,
        )

    def _append(self, events: list[MutationEvent]) -> None:
        if not events:
            return
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.log_path.open("a", encoding="utf-8") as handle:
            for event in events:
                handle.write(json.dumps(asdict(event), separators=(",", ":")) + "\n")

    def _git(self, *args: str) -> str | None:
        result = subprocess.run(
            ["git", *args],
            cwd=self.workspace,
            text=True,
            capture_output=True,
            check=False,
            env=isolated_subprocess_environment(
                {
                    "GIT_CONFIG_NOSYSTEM": "1",
                    "GIT_CONFIG_GLOBAL": str(Path(os.devnull)),
                    "GIT_ATTR_NOSYSTEM": "1",
                }
            ),
            **isolated_process_kwargs(),
        )
        return result.stdout.strip() if result.returncode == 0 else None

    def _git_lines(self, *args: str) -> list[str]:
        value = self._git(*args)
        return value.splitlines() if value else []

    @staticmethod
    def _hash(content: bytes) -> str:
        return hashlib.sha256(content).hexdigest()

    @staticmethod
    def _decode(content: bytes) -> str:
        return content.decode("utf-8", errors="replace")

    @staticmethod
    def _diff(path: str, before: str | None, after: str | None) -> str:
        return "".join(unified_diff((before or "").splitlines(keepends=True), (after or "").splitlines(keepends=True), fromfile=f"a/{path}", tofile=f"b/{path}"))
