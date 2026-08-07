from __future__ import annotations

import sys
from pathlib import Path

from pulse.audit import AuditLog
from pulse.config import SandboxConfig
from pulse.mutations import MutationTracker


class ProjectSandbox:
    def __init__(self, config: SandboxConfig, audit: AuditLog, mutations: MutationTracker | None = None) -> None:
        self.config = config
        self.audit = audit
        self.mutations = mutations or MutationTracker(config.workspace_root)

    def list_files(self) -> list[str]:
        ignored = {".git", ".agent", ".agents", ".venv", "__pycache__"}
        files: list[str] = []

        for path in self.config.workspace_root.rglob("*"):
            if any(part in ignored for part in path.relative_to(self.config.workspace_root).parts):
                continue
            if path.is_file():
                files.append(self._display_path(path))

        return sorted(files)

    def read_file(self, file: str, reason: str, *, auto_approve: bool = False) -> str | None:
        if self.config.require_permission_for_reads and not auto_approve and not self._ask_permission("read", file, reason):
            self.audit.record("read-denied", file, "User denied read permission.")
            return None

        path = self._assert_inside_workspace(file)
        content = path.read_text(encoding="utf-8", errors="replace")
        self.audit.record("read", file, "Read file with permission.")
        return content[:12_000]

    def request_project_action(self, action: str, file: str, reason: str) -> bool:
        if self.config.require_permission_for_project_actions and not self._ask_permission(action, file, reason):
            self.audit.record(f"{action}-denied", file, "User denied project action.")
            return False

        self.audit.record(action, file, reason)
        return True

    def write_file(self, file: str, content: str, reason: str) -> bool:
        if not self.config.allow_writes:
            self.audit.record("edit-blocked", file, "Writes are disabled by sandbox config.")
            print(f"Writes are disabled. Skipped edit on {file}.")
            return False

        if not self.request_project_action("edit", file, reason):
            return False

        path = self._assert_inside_workspace(file)
        with self.mutations.transaction():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        self.audit.record("edited", file, "Edited file with permission.")
        return True

    def read_file_for_edit(self, file: str) -> str | None:
        """Read the complete current text for a proposed edit without prompting.

        The proposal is not an action and the caller must still obtain explicit
        approval before ``apply_approved_edit`` can write it.
        """
        path = self._assert_inside_workspace(file)
        return path.read_text(encoding="utf-8", errors="replace") if path.exists() else None

    def apply_approved_edit(self, file: str, content: str, reason: str) -> None:
        """Apply an edit already approved by the edit workflow.

        This deliberately does not consult ``allow_writes``: that setting keeps
        unapproved/direct writes off by default, while this narrow method is
        protected by the workflow's per-edit approval.
        """
        path = self._assert_inside_workspace(file)
        with self.mutations.transaction(command="pulse approved edit"):
            path.parent.mkdir(parents=True, exist_ok=True)
            self._write_text_exact(path, content)
        self.audit.record("edited", file, f"Approved edit: {reason}")

    def record_rejected_edit(self, file: str, reason: str) -> None:
        self.audit.record("edit-rejected", file, reason)

    def rollback_last_approved_edit(self) -> bool:
        events = self.mutations.last_approved_edit()
        if not events:
            return False
        with self.mutations.transaction(command="pulse rollback"):
            for event in events:
                self._restore_event(event)
        self.audit.record("rollback", ", ".join(str(event["file_path"]) for event in events), "Restored last approved edit.")
        return True

    def _restore_event(self, event: dict[str, object]) -> None:
        file = str(event["file_path"])
        if " -> " in file:
            raise ValueError("Rollback does not support renamed files.")
        path = self._assert_inside_workspace(file)
        before = event.get("before_content")
        if before is None:
            if path.exists():
                path.unlink()
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        self._write_text_exact(path, str(before))

    @staticmethod
    def _write_text_exact(path: Path, content: str) -> None:
        """Avoid Windows newline conversion when restoring a tracked snapshot."""
        with path.open("w", encoding="utf-8", newline="") as handle:
            handle.write(content)

    def delete_file(self, file: str, reason: str) -> bool:
        if not self.config.allow_writes:
            self.audit.record("delete-blocked", file, "Writes are disabled by sandbox config.")
            return False
        if not self.request_project_action("delete", file, reason):
            return False
        path = self._assert_inside_workspace(file)
        with self.mutations.transaction():
            path.unlink()
        self.audit.record("deleted", file, "Deleted file with permission.")
        return True

    def rename_file(self, source: str, destination: str, reason: str) -> bool:
        if not self.config.allow_writes:
            self.audit.record("rename-blocked", source, "Writes are disabled by sandbox config.")
            return False
        if not self.request_project_action("rename", source, reason):
            return False
        source_path = self._assert_inside_workspace(source)
        destination_path = self._assert_inside_workspace(destination)
        with self.mutations.transaction():
            destination_path.parent.mkdir(parents=True, exist_ok=True)
            source_path.rename(destination_path)
        self.audit.record("renamed", f"{source} -> {destination}", "Renamed file with permission.")
        return True

    def _ask_permission(self, action: str, file: str, reason: str) -> bool:
        if not sys.stdin.isatty():
            print(f"Denied {action} on {file}: no interactive terminal available for permission.")
            return False

        answer = input(f"Allow {action} on {file}? {reason} [y/N] ").strip().lower()
        return answer in {"y", "yes"}

    def _assert_inside_workspace(self, file: str) -> Path:
        path = (self.config.workspace_root / file).resolve()
        if path != self.config.workspace_root and self.config.workspace_root not in path.parents:
            raise ValueError(f"Path is outside workspace: {file}")
        return path

    def _display_path(self, path: Path) -> str:
        return str(path.relative_to(self.config.workspace_root))
