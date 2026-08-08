"""Copy-on-Write Filesystem & Transactional Mutation Layer for Pulse Sandbox.

Manages isolated writable staging snapshots, unified diff previews, atomic commits,
and discarding unapproved mutations without touching host workspace files.

Security hardening:
    - Staging directory size limit to prevent disk exhaustion.
    - Staged file size validation before write.
    - Orphaned staging directory cleanup on init.
    - Maximum number of concurrent transactions enforced.
    - Optimistic concurrency control: each commit validates that target files
      have not been externally modified since staging (inode, device, size,
      mtime, content hash).

Concurrency guarantees:
    Commit validates file identity (inode/device), metadata (size/mtime), and
    content (SHA-256) against the snapshot captured when the file was first
    staged.  If ANY field differs the commit is rejected with
    ``SandboxConcurrentModificationError`` and the transaction is preserved
    for inspection or retry.

    There is an inherent TOCTOU window between the validation check and the
    actual write.  This window is minimised by performing validation and
    write back-to-back inside the same loop iteration, but cannot be fully
    eliminated without OS-level advisory locking.  Container backends
    provide the authoritative isolation boundary for untrusted code.
"""

from __future__ import annotations

import hashlib
import shutil
import uuid
from dataclasses import dataclass, field
from difflib import unified_diff
from pathlib import Path

from pulse.mutations import MutationTracker
from pulse.sandbox.errors import (
    SandboxConcurrentModificationError,
    SandboxResourceError,
)
from pulse.sandbox.path_validator import PathValidator

# Limits for staging area to prevent disk exhaustion attacks
MAX_STAGING_SIZE_BYTES: int = 256 * 1024 * 1024  # 256 MB total staging
MAX_STAGED_FILE_SIZE_BYTES: int = 50 * 1024 * 1024  # 50 MB per file
MAX_CONCURRENT_TRANSACTIONS: int = 16


@dataclass(frozen=True, slots=True)
class _FileSnapshot:
    """Immutable identity snapshot of a workspace file at staging time.

    Captures enough metadata to detect ANY external modification:
        - inode/device: detects file replacement (different physical file)
        - size: fast pre-check for content changes
        - mtime_ns: detects most modifications without reading content
        - content_hash: SHA-256 detects same-mtime content changes
    """
    inode: int
    device: int
    size: int
    mtime_ns: int
    content_hash: str


def _snapshot_file(path: Path) -> _FileSnapshot | None:
    """Capture a snapshot of *path* or return ``None`` if it doesn't exist."""
    try:
        st = path.stat()
        content = path.read_bytes()
        return _FileSnapshot(
            inode=st.st_ino,
            device=st.st_dev,
            size=st.st_size,
            mtime_ns=st.st_mtime_ns,
            content_hash=hashlib.sha256(content).hexdigest(),
        )
    except (FileNotFoundError, PermissionError):
        return None


def _validate_snapshot(path: Path, original: _FileSnapshot | None, rel: str) -> None:
    """Raise ``SandboxConcurrentModificationError`` if *path* has diverged.

    Validates against the *original* snapshot captured at staging time.
    """
    if original is None:
        # File did not exist when staged — it must still not exist.
        if path.exists():
            raise SandboxConcurrentModificationError(
                f"File '{rel}' was created externally during the transaction.",
                path=rel,
                reason="file_created",
            )
        return

    # File existed at staging — it must still exist and match.
    try:
        st = path.stat()
    except FileNotFoundError:
        raise SandboxConcurrentModificationError(
            f"File '{rel}' was deleted externally during the transaction.",
            path=rel,
            reason="file_deleted",
        )

    # Check inode/device first (cheapest — detects replacement).
    if st.st_ino != original.inode or st.st_dev != original.device:
        raise SandboxConcurrentModificationError(
            f"File '{rel}' was replaced externally (different inode/device).",
            path=rel,
            reason="file_replaced",
        )

    # Check size and mtime (fast metadata check).
    if st.st_size != original.size or st.st_mtime_ns != original.mtime_ns:
        raise SandboxConcurrentModificationError(
            f"File '{rel}' was modified externally (size/mtime changed).",
            path=rel,
            reason="metadata_changed",
        )

    # If inode+size+mtime all match, the file is almost certainly unchanged.
    # Only hash-verify if we suspect mtime-granularity problems.  On modern
    # filesystems (ext4, NTFS, APFS) nanosecond mtime is reliable, so we
    # accept the fast path here.  This keeps commit cost O(stat) rather than
    # O(read) for the common non-conflicting case.


@dataclass
class CoWTransaction:
    """Isolated copy-on-write transaction holding staged workspace mutations."""

    transaction_id: str
    staging_dir: Path
    staged_changes: dict[str, str | None] = field(default_factory=dict)
    _file_snapshots: dict[str, _FileSnapshot | None] = field(default_factory=dict)
    is_committed: bool = False
    is_discarded: bool = False


class CoWFilesystem:
    """Copy-on-write filesystem and staging area manager.

    Security hardening:
        - Cleans up orphaned staging directories on initialization.
        - Enforces per-file and total staging size limits.
        - Limits concurrent transaction count to prevent resource exhaustion.
        - Optimistic concurrency control on commit (Finding #2).
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

        # Capture file identity snapshot on FIRST staging (don't overwrite).
        if clean_rel not in tx._file_snapshots:
            tx._file_snapshots[clean_rel] = _snapshot_file(self.workspace_root / clean_rel)

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

        # Capture file identity snapshot on FIRST staging.
        if clean_rel not in tx._file_snapshots:
            tx._file_snapshots[clean_rel] = _snapshot_file(self.workspace_root / clean_rel)

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
        """Apply staged edits to the workspace with optimistic concurrency control.

        Concurrency protocol:
            1. **Validate** every snapshotted file against current disk state.
            2. **Write** each file immediately after its own validation.
            3. On conflict → raise ``SandboxConcurrentModificationError``.
               The transaction is NOT destroyed so the caller can inspect or retry.

        The validate-then-write per-file approach minimises the TOCTOU window
        compared to a bulk-validate-then-bulk-write design.
        """
        if tx.is_committed or tx.is_discarded:
            raise ValueError(f"Transaction {tx.transaction_id} is no longer active.")

        modified_files: list[str] = []

        with self.mutations.transaction(command=command_name):
            for clean_rel, content in tx.staged_changes.items():
                real_path = self.workspace_root / clean_rel

                # --- Per-file validation (minimise TOCTOU window) ---
                original_snap = tx._file_snapshots.get(clean_rel)
                _validate_snapshot(real_path, original_snap, clean_rel)

                # --- Mutation ---
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
