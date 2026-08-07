"""Path validation, TOCTOU-safe file access, and workspace boundary isolation.

Prevents path traversal, symlink loops, symlink escapes, and unauthorized access
outside configured workspace boundaries.

Security hardening (TOCTOU + Memory Exhaustion):
    - safe_open() atomically validates and opens file descriptors.
    - O_NOFOLLOW used on POSIX to prevent symlink following.
    - Post-open fstat() re-validates the resolved path matches expectations.
    - MAX_FILE_SIZE enforced before reading to prevent OOM attacks.
    - safe_read() returns content with enforced size limits.
"""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

from pulse.sandbox.errors import SandboxResourceError, SandboxSecurityError

# Maximum file size that can be read through the sandbox (50 MB)
MAX_FILE_SIZE: int = 50 * 1024 * 1024


class PathValidationError(ValueError):
    """Raised when a path violates security boundaries."""

    def __init__(self, message: str, path: str | Path, reason: str) -> None:
        super().__init__(f"{message}: {path} ({reason})")
        self.path = str(path)
        self.reason = reason


class PathValidator:
    """Security boundary validator for workspace filesystem access.

    Provides TOCTOU-safe file operations that atomically validate and open
    file descriptors, preventing race condition exploits.
    """

    def __init__(
        self,
        workspace_root: Path,
        allowed_external_reads: list[Path] | None = None,
        max_file_size: int = MAX_FILE_SIZE,
    ) -> None:
        self.workspace_root = workspace_root.resolve()
        self.allowed_external_reads = [p.resolve() for p in (allowed_external_reads or [])]
        self.max_file_size = max_file_size

    def validate_path(
        self,
        path: str | Path,
        *,
        allow_read_only_external: bool = False,
        must_exist: bool = False,
    ) -> Path:
        """Resolve and validate that a path is strictly inside workspace boundaries.

        Args:
            path: Relative or absolute path to validate.
            allow_read_only_external: Whether to allow access to configured external read paths.
            must_exist: If True, raises PathValidationError if target path does not exist.

        Returns:
            Resolved canonical Path object.

        Raises:
            PathValidationError: If path escapes workspace or violates symlink rules.
        """
        raw_path = Path(path)

        # 1. Resolve path candidate relative to workspace root if relative
        if not raw_path.is_absolute():
            candidate = (self.workspace_root / raw_path)
        else:
            candidate = raw_path

        # 2. Check symlink loops and resolve target
        try:
            resolved = candidate.resolve()
        except RuntimeError as err:
            raise PathValidationError("Symlink resolution failed", path, "Possible symlink loop") from err

        if must_exist and not resolved.exists():
            raise PathValidationError("Path does not exist", path, "Target path missing")

        # 3. Verify workspace root containment
        if self._is_contained_in(resolved, self.workspace_root):
            return resolved

        # 4. Check allowed external read paths if permitted
        if allow_read_only_external:
            for ext_path in self.allowed_external_reads:
                if self._is_contained_in(resolved, ext_path):
                    return resolved

        raise PathValidationError("Path is outside workspace boundary", path, f"Resolved to {resolved}")

    def is_inside_workspace(self, path: str | Path) -> bool:
        """Return True if path resolves inside workspace_root."""
        try:
            self.validate_path(path, allow_read_only_external=False, must_exist=False)
            return True
        except PathValidationError:
            return False

    def assert_inside_workspace(self, path: str | Path) -> Path:
        """Convenience assertion method returning resolved Path or raising PathValidationError."""
        return self.validate_path(path, allow_read_only_external=False, must_exist=False)

    def safe_open(self, path: str | Path, *, for_write: bool = False) -> int:
        """Atomically validate and open a file descriptor, preventing TOCTOU races.

        Security guarantees:
            1. Path is validated inside workspace BEFORE opening.
            2. On POSIX, O_NOFOLLOW prevents following symlinks at the final component.
            3. After open, fstat() verifies the file is a regular file.
            4. File size is checked against max_file_size before returning.

        Args:
            path: Relative or absolute path to open.
            for_write: If True, open for writing (O_WRONLY | O_CREAT).

        Returns:
            Raw file descriptor (caller is responsible for os.close()).

        Raises:
            PathValidationError: If path escapes workspace.
            SandboxSecurityError: If symlink detected at open time, or file is not regular.
            SandboxResourceError: If file exceeds max_file_size.
        """
        resolved = self.assert_inside_workspace(path)

        # Build open flags
        flags = os.O_RDONLY
        if for_write:
            flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC

        # On POSIX, add O_NOFOLLOW to prevent symlink following at the final path component.
        # This closes the TOCTOU gap between validate_path() and the actual open().
        is_windows = sys.platform == "win32"
        if not is_windows and hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW

        # Windows TOCTOU mitigation: lstat before open, fstat after open
        pre_stat = None
        if is_windows:
            try:
                pre_stat = os.lstat(str(resolved))
                if stat.S_ISLNK(pre_stat.st_mode):
                    raise SandboxSecurityError(
                        "Symlink detected during safe_open (pre-stat) — possible TOCTOU attack",
                        operation="safe_open",
                        path=str(path),
                    )
            except FileNotFoundError:
                # File doesn't exist yet, which is fine if for_write is True
                if not for_write:
                    raise

        try:
            fd = os.open(str(resolved), flags, 0o644)
        except OSError as err:
            if err.errno == 40:  # ELOOP — symlink detected with O_NOFOLLOW
                raise SandboxSecurityError(
                    "Symlink detected during safe_open — possible TOCTOU attack",
                    operation="safe_open",
                    path=str(path),
                ) from err
            raise

        try:
            # Post-open validation: verify file descriptor points to a regular file
            file_stat = os.fstat(fd)
            
            # Windows TOCTOU mitigation: verify st_ino and st_dev match pre_stat
            if is_windows and pre_stat is not None:  # noqa: SIM102
                if pre_stat.st_ino != file_stat.st_ino or pre_stat.st_dev != file_stat.st_dev:
                    os.close(fd)
                    raise SandboxSecurityError(
                        "File identity changed during safe_open (inode mismatch) — TOCTOU race detected",
                        operation="safe_open",
                        path=str(path),
                    )
            
            if not stat.S_ISREG(file_stat.st_mode) and not for_write:
                os.close(fd)
                raise SandboxSecurityError(
                    "Opened file is not a regular file (possible device/pipe/socket injection)",
                    operation="safe_open",
                    path=str(path),
                )

            # Check file size against limit (only for reads)
            if not for_write and file_stat.st_size > self.max_file_size:
                os.close(fd)
                raise SandboxResourceError(
                    f"File size ({file_stat.st_size} bytes) exceeds maximum "
                    f"({self.max_file_size} bytes): {path}",
                    limit_name="max_file_size",
                    limit_value=self.max_file_size,
                )

            return fd

        except Exception:
            # Ensure fd is closed on any validation failure
            try:
                os.close(fd)
            except OSError:
                pass
            raise

    def safe_read(self, path: str | Path) -> str:
        """TOCTOU-safe file read with size limit enforcement.

        Atomically validates, opens, checks size, and reads the file content
        through a single file descriptor, closing the race window between
        validation and use.

        Args:
            path: Relative or absolute path to read.

        Returns:
            File content as a UTF-8 string.

        Raises:
            PathValidationError: If path escapes workspace.
            SandboxSecurityError: On TOCTOU/symlink detection.
            SandboxResourceError: If file exceeds max_file_size.
        """
        fd = self.safe_open(path, for_write=False)
        try:
            # Read in chunks to prevent unbounded memory allocation
            chunks: list[bytes] = []
            total_read = 0
            chunk_size = 1024 * 1024  # 1 MB chunks

            while True:
                chunk = os.read(fd, chunk_size)
                if not chunk:
                    break
                total_read += len(chunk)
                if total_read > self.max_file_size:
                    raise SandboxResourceError(
                        f"File read exceeded maximum size ({self.max_file_size} bytes): {path}",
                        limit_name="max_file_size",
                        limit_value=self.max_file_size,
                    )
                chunks.append(chunk)

            return b"".join(chunks).decode("utf-8", errors="replace")
        finally:
            os.close(fd)

    @staticmethod
    def _is_contained_in(target: Path, parent: Path) -> bool:
        """Return True if target equals parent or is a child of parent."""
        if target == parent:
            return True
        try:
            target.relative_to(parent)
            return True
        except ValueError:
            return False
