from __future__ import annotations

import sys
from pathlib import Path

from pulse.audit import AuditLog
from pulse.config import SandboxConfig


class ProjectSandbox:
    def __init__(self, config: SandboxConfig, audit: AuditLog) -> None:
        self.config = config
        self.audit = audit

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
        path.write_text(content, encoding="utf-8")
        self.audit.record("edited", file, "Edited file with permission.")
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
