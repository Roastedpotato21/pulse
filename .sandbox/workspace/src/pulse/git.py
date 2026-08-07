"""Async Git inspection and commit guidance, independent of Pulse interfaces."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class GitChange:
    path: str
    index_status: str
    worktree_status: str


@dataclass(frozen=True, slots=True)
class GitStatus:
    is_repository: bool
    branch: str | None = None
    head: str | None = None
    changes: tuple[GitChange, ...] = ()


@dataclass(frozen=True, slots=True)
class DiffAnalysis:
    files_changed: int
    additions: int
    deletions: int
    files: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class GitInsight:
    status: GitStatus
    diff: DiffAnalysis
    commit_suggestion: str | None


GitRunner = Callable[[tuple[str, ...], Path], Awaitable[tuple[int, str, str]]]


class GitIntelligence:
    """Read-only Git state, diff analysis, and conventional commit suggestions."""

    def __init__(self, workspace: Path, *, runner: GitRunner | None = None) -> None:
        self.workspace = workspace.resolve()
        self._runner = runner or self._run_git

    async def status(self) -> GitStatus:
        code, output, _ = await self._runner(("rev-parse", "--is-inside-work-tree"), self.workspace)
        if code != 0 or output.strip() != "true":
            return GitStatus(False)
        _, branch, _ = await self._runner(("branch", "--show-current"), self.workspace)
        _, head, _ = await self._runner(("rev-parse", "--short", "HEAD"), self.workspace)
        _, porcelain, _ = await self._runner(("status", "--porcelain=v1"), self.workspace)
        return GitStatus(True, branch.strip() or None, head.strip() or None, self._parse_status(porcelain))

    async def analyze_diff(self) -> DiffAnalysis:
        status = await self.status()
        if not status.is_repository:
            return DiffAnalysis(0, 0, 0)
        _, unstaged, _ = await self._runner(("diff", "--numstat"), self.workspace)
        _, staged, _ = await self._runner(("diff", "--cached", "--numstat"), self.workspace)
        additions = deletions = 0
        files = {change.path for change in status.changes}
        for line in f"{unstaged}\n{staged}".splitlines():
            parts = line.split("\t", 2)
            if len(parts) != 3:
                continue
            added, removed, path = parts
            additions += int(added) if added.isdigit() else 0
            deletions += int(removed) if removed.isdigit() else 0
            files.add(path)
        return DiffAnalysis(len(files), additions, deletions, tuple(sorted(files)))

    async def inspect(self) -> GitInsight:
        """Return branch, working-tree changes, diff totals, and a commit idea."""
        status = await self.status()
        if not status.is_repository:
            return GitInsight(status, DiffAnalysis(0, 0, 0), None)
        # Reuse the status we already collected while keeping diff collection
        # independent and safe for callers that only need one of the operations.
        _, unstaged, _ = await self._runner(("diff", "--numstat"), self.workspace)
        _, staged, _ = await self._runner(("diff", "--cached", "--numstat"), self.workspace)
        additions = deletions = 0
        files = {change.path for change in status.changes}
        for line in f"{unstaged}\n{staged}".splitlines():
            parts = line.split("\t", 2)
            if len(parts) == 3:
                additions += int(parts[0]) if parts[0].isdigit() else 0
                deletions += int(parts[1]) if parts[1].isdigit() else 0
                files.add(parts[2])
        diff = DiffAnalysis(len(files), additions, deletions, tuple(sorted(files)))
        return GitInsight(status, diff, self.suggest_commit(status, diff))

    @staticmethod
    def suggest_commit(status: GitStatus, diff: DiffAnalysis) -> str | None:
        if not status.is_repository or not diff.files:
            return None
        paths = diff.files
        if all(path.lower().endswith((".md", ".rst")) or "docs/" in path.lower() for path in paths):
            kind = "docs"
        elif all("test" in Path(path).name.lower() for path in paths):
            kind = "test"
        elif any(change.index_status == "A" or change.worktree_status == "?" for change in status.changes):
            kind = "feat"
        else:
            kind = "chore"
        subject = paths[0] if len(paths) == 1 else f"{len(paths)} files"
        return f"{kind}: update {subject}"

    @staticmethod
    def _parse_status(output: str) -> tuple[GitChange, ...]:
        changes: list[GitChange] = []
        for line in output.splitlines():
            if len(line) < 4:
                continue
            changes.append(GitChange(line[3:], line[0], line[1]))
        return tuple(changes)

    @staticmethod
    async def _run_git(arguments: tuple[str, ...], workspace: Path) -> tuple[int, str, str]:
        try:
            process = await asyncio.create_subprocess_exec(
                "git", *arguments, cwd=workspace, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
        except OSError as error:
            return 127, "", str(error)
        stdout, stderr = await process.communicate()
        return process.returncode, stdout.decode(errors="replace"), stderr.decode(errors="replace")
