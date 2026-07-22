"""Approval-gated file editing, independent from any user interface."""

from __future__ import annotations

from dataclasses import dataclass
from difflib import unified_diff
from typing import Awaitable, Callable

from pulse.sandbox import ProjectSandbox


@dataclass(frozen=True)
class EditProposal:
    file_path: str
    before_content: str | None
    after_content: str
    reason: str
    unified_diff: str


@dataclass(frozen=True)
class EditResult:
    proposal: EditProposal
    applied: bool


ApprovalHandler = Callable[[EditProposal], Awaitable[bool]]


class EditWorkflow:
    """Creates diffs first and mutates a project only after explicit approval."""

    def __init__(self, sandbox: ProjectSandbox) -> None:
        self.sandbox = sandbox

    async def propose(self, file_path: str, content: str, reason: str) -> EditProposal:
        before = self.sandbox.read_file_for_edit(file_path)
        return EditProposal(
            file_path=file_path,
            before_content=before,
            after_content=content,
            reason=reason,
            unified_diff="".join(
                unified_diff(
                    (before or "").splitlines(keepends=True),
                    content.splitlines(keepends=True),
                    fromfile=f"a/{file_path}",
                    tofile=f"b/{file_path}",
                )
            ),
        )

    async def request_and_apply(
        self, file_path: str, content: str, reason: str, approve: ApprovalHandler
    ) -> EditResult:
        proposal = await self.propose(file_path, content, reason)
        if not await approve(proposal):
            self.sandbox.record_rejected_edit(proposal.file_path, proposal.reason)
            return EditResult(proposal=proposal, applied=False)

        self.sandbox.apply_approved_edit(proposal.file_path, proposal.after_content, proposal.reason)
        return EditResult(proposal=proposal, applied=True)

    async def rollback_last(self) -> bool:
        return self.sandbox.rollback_last_approved_edit()
