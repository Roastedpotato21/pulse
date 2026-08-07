import asyncio
import os
from pathlib import Path

from pulse.audit import AuditLog
from pulse.config import SandboxConfig
from pulse.edits import EditWorkflow
from pulse.sandbox import ProjectSandbox


def workflow(tmp_path: Path) -> EditWorkflow:
    sandbox = ProjectSandbox(
        SandboxConfig(tmp_path, False, True, False),
        AuditLog(tmp_path / ".agent" / "logs" / "actions.jsonl"),
    )
    return EditWorkflow(sandbox)


async def approve(_proposal) -> bool:
    return True


async def reject(_proposal) -> bool:
    return False


def test_approved_edit_is_applied_and_tracked(tmp_path: Path) -> None:
    target = tmp_path / "note.txt"
    target.write_text("before\n", encoding="utf-8")
    flow = workflow(tmp_path)

    result = asyncio.run(flow.request_and_apply("note.txt", "after\n", "update note", approve))

    assert result.applied is True
    assert "-before" in result.proposal.unified_diff
    assert target.read_text(encoding="utf-8") == "after\n"
    event = list(flow.sandbox.mutations.history())[-1]
    assert event["command"] == "pulse approved edit"
    assert event["timestamp"] and event["before_content"] == f"before{os.linesep}"


def test_rejected_edit_is_discarded(tmp_path: Path) -> None:
    target = tmp_path / "note.txt"
    target.write_text("before\n", encoding="utf-8")

    result = asyncio.run(workflow(tmp_path).request_and_apply("note.txt", "after\n", "update note", reject))

    assert result.applied is False
    assert target.read_text(encoding="utf-8") == "before\n"


def test_rollback_restores_last_approved_edit(tmp_path: Path) -> None:
    target = tmp_path / "note.txt"
    target.write_text("before\n", encoding="utf-8")
    flow = workflow(tmp_path)
    asyncio.run(flow.request_and_apply("note.txt", "after\n", "update note", approve))

    assert asyncio.run(flow.rollback_last()) is True
    assert target.read_text(encoding="utf-8") == "before\n"
