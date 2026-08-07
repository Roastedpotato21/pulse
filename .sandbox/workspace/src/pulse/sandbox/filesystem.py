"""Copy-on-Write Filesystem & Transactional Mutation Layer for Pulse Sandbox.

Manages isolated writable staging snapshots, unified diff previews, atomic commits,
and discarding unapproved mutations without touching host workspace files.

Security hardening:
    - Staging directory size limit to prevent disk exhaustion.
    - Staged file size validation before write.
    - Orphaned staging directory cleanup on init.
    - Maximum number of concurrent transactions enforced.
"""

from __future__ import annotations

import shutil
import uuid
from dataclasses import dataclass, field
from difflib import unified_diff
from pathlib import Path

from pulse.mutations import MutationTracker
from pulse.sandbox.errors import SandboxResourceError
from pulse.sandbox.path_validator import PathValidator

# Limits for staging area to prevent disk exhaustion attacks
MAX_STAGING_SIZE_BYTES: int = 256 * 1024 * 1024  # 256 MB total staging
MAX_STAGED_FILE_SIZE_BYTES: int = 50 * 1024 * 1024  # 50 MB per file
MAX_CONCURRENT_TRANSACTIONS: int = 16


@dataclass
class CoWTransaction:
    """Isolated copy-on-write transaction holding staged workspace mutations."""

    transaction_id: str
    staging_dir: Path
    staged_changes: dict[str, str | None] = field(default_factory=dict)
    is_committed: bool = False
    is_discarded: bool = False


class CoWFilesystem:
    """Copy-on-write filesystem and staging area manager.

    Security hardening:
        - Cleans up orphaned staging directories on initialization.
        - Enforces per-file and total staging size limits.
        - Limits concurrent transaction count to prevent resource exhaustion.
    """

    def __init__(
        self,
        workspace_root: Path,
        mutations: MutationTracker | None = None,
        max_staging_bytes: int = MAX_STAGING_SIZE_BYTES,
        max_file_bytes: int = MAX_STAGED_FILE_SIZE_BYTES,
    ) -> None:
        self.workspace_root = workspace_root.resolve()
        self.validator = PathValidator(self.workspace_root)
        self.mutations = mutations or MutationTracker(self.workspace_root)
        self._staging_base = self.workspace_root / ".agent" / "staging"
        self._max_staging_bytes = max_staging_bytes
        self._max_file_bytes = max_file_bytes
        self._active_transactions: dict[str, CoWTransaction] = {}

        # Clean up orphaned staging directories from previous crashes
        self._cleanup_orphaned_staging()

    def _cleanup_orphaned_staging(self) -> None:
        """Remove any leftover staging directories from previous sessions.

        Security rationale:
            Orphaned staging directories from crashed sessions could contain
            partially committed changes or consume disk space indefinitely.
        """
        if self._staging_base.exists():
            for child in self._staging_base.iterdir():
                if child.is_dir():
                    shutil.rmtree(child, ignore_errors=True)

    def _check_staging_size(self, additional_bytes: int = 0) -> None:
        """Verify total staging area hasn't exceeded disk limit."""
        if not self._staging_base.exists():
            return

        total = sum(
            f.stat().st_size
            for f in self._staging_base.rglob("*")
            if f.is_file()
        )

        if total + additional_bytes > self._max_staging_bytes:
            raise SandboxResourceError(
                f"Staging area size ({total + additional_bytes} bytes) exceeds "
                f"maximum ({self._max_staging_bytes} bytes). "
                "Commit or discard existing transactions to free space.",
                limit_name="max_staging_bytes",
                limit_value=self._max_staging_bytes,
            )

    def create_transaction(self) -> CoWTransaction:
        """Create a new isolated staging directory for CoW mutations."""
        if len(self._active_transactions) >= MAX_CONCURRENT_TRANSACTIONS:
            raise SandboxResourceError(
                f"Maximum concurrent transactions ({MAX_CONCURRENT_TRANSACTIONS}) exceeded. "
                "Commit or discard existing transactions first.",
                limit_name="max_concurrent_transactions",
                limit_value=MAX_CONCURRENT_TRANSACTIONS,
            )

        tx_id = str(uuid.uuid4())[:8]
        staging_dir = self._staging_base / tx_id
        staging_dir.mkdir(parents=True, exist_ok=True)
        tx = CoWTransaction(transaction_id=tx_id, staging_dir=staging_dir)
        self._active_transactions[tx_id] = tx
        return tx

    def stage_write(self, tx: CoWTransaction, relative_path: str, content: str) -> Path:
        """Stage a file write inside the CoW transaction staging directory."""
        if tx.is_committed or tx.is_discarded:
            raise ValueError(f"Transaction {tx.transaction_id} is no longer active.")

        # Validate file size
        content_bytes = content.encode("utf-8")
        if len(content_bytes) > self._max_file_bytes:
            raise SandboxResourceError(
                f"Staged file size ({len(content_bytes)} bytes) exceeds "
                f"maximum ({self._max_file_bytes} bytes): {relative_path}",
                limit_name="max_staged_file_size",
                limit_value=self._max_file_bytes,
            )

        # Check total staging area capacity
        self._check_staging_size(len(content_bytes))

        clean_rel = self.validator.assert_inside_workspace(relative_path).relative_to(self.workspace_root).as_posix()
        staged_path = tx.staging_dir / clean_rel
        staged_path.parent.mkdir(parents=True, exist_ok=True)
        staged_path.write_text(content, encoding="utf-8")

        tx.staged_changes[clean_rel] = content
        return staged_path

    def stage_delete(self, tx: CoWTransaction, relative_path: str) -> None:
        """Stage a file deletion inside the CoW transaction."""
        if tx.is_committed or tx.is_discarded:
            raise ValueError(f"Transaction {tx.transaction_id} is no longer active.")

        clean_rel = self.validator.assert_inside_workspace(relative_path).relative_to(self.workspace_root).as_posix()
        tx.staged_changes[clean_rel] = None  # None indicates deletion

    def preview_changes(self, tx: CoWTransaction) -> str:
        """Generate unified diff preview of all staged mutations in the transaction."""
        diff_lines: list[str] = []

        for clean_rel, after_content in sorted(tx.staged_changes.items()):
            real_path = self.workspace_root / clean_rel
            before_content = real_path.read_text(encoding="utf-8", errors="replace") if real_path.exists() else ""

            if after_content is None:
                # File deletion
                after_content_str = ""
            else:
                after_content_str = after_content

            diff_str = "".join(
                unified_diff(
                    before_content.splitlines(keepends=True),
                    after_content_str.splitlines(keepends=True),
                    fromfile=f"a/{clean_rel}",
                    tofile=f"b/{clean_rel}",
                )
            )
            if diff_str:
                diff_lines.append(diff_str)

        return "".join(diff_lines)

    def commit_transaction(self, tx: CoWTransaction, command_name: str = "pulse cow commit") -> list[str]:
        """Apply all staged edits atomically to the workspace inside a MutationTracker transaction."""
        if tx.is_committed or tx.is_discarded:
            raise ValueError(f"Transaction {tx.transaction_id} is no longer active.")

        modified_files: list[str] = []

        with self.mutations.transaction(command=command_name):
            for clean_rel, content in tx.staged_changes.items():
                real_path = self.workspace_root / clean_rel

                if content is None:
                    # Execute staged deletion
                    if real_path.exists():
                        real_path.unlink()
                else:
                    # Execute staged write
                    real_path.parent.mkdir(parents=True, exist_ok=True)
                    real_path.write_text(content, encoding="utf-8")

                modified_files.append(clean_rel)

        tx.is_committed = True
        self._active_transactions.pop(tx.transaction_id, None)
        self.discard_transaction(tx)  # Clean up staging directory
        return modified_files

    def discard_transaction(self, tx: CoWTransaction) -> None:
        """Discard staged changes and delete temporary staging directory."""
        if tx.staging_dir.exists():
            shutil.rmtree(tx.staging_dir, ignore_errors=True)
        tx.is_discarded = True
        self._active_transactions.pop(tx.transaction_id, None)
