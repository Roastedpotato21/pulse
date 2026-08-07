"""Unit tests for the Pulse Task Manager (pulse.task_manager).

Tests cover:
- Task creation, field defaults, and priority mapping
- Priority queue ordering and dependency resolution
- Lifecycle state transitions (pending, queued, running, paused, resumed, completed, cancelled, failed)
- Checkpoint creation and restoration
- EventBus subscription and emission
- File-backed TaskStore persistence across sessions
- TaskTool integration with ToolInvocation
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from pulse.task_manager import (
    Task,
    TaskEvent,
    TaskEventBus,
    TaskManager,
    TaskPriority,
    TaskStatus,
)
from pulse.tool_registry import ToolInvocation

# ---------------------------------------------------------------------------
# 1. Task Model & Serialization
# ---------------------------------------------------------------------------


def test_task_model_serialization(tmp_path: Path) -> None:
    task = Task(
        id="task-100",
        title="Refactor Planner",
        goal="Refactor DAG planner execution",
        priority=TaskPriority.HIGH,
        status=TaskStatus.PENDING,
        depends_on=["task-001"],
    )

    data = task.to_dict()
    assert data["id"] == "task-100"
    assert data["priority"] == "HIGH"
    assert data["status"] == "PENDING"
    assert data["depends_on"] == ["task-001"]

    deserialized = Task.from_dict(data)
    assert deserialized.id == task.id
    assert deserialized.priority == TaskPriority.HIGH
    assert deserialized.status == TaskStatus.PENDING


# ---------------------------------------------------------------------------
# 2. Priority Ordering & Task Queueing
# ---------------------------------------------------------------------------


def test_priority_ordering(tmp_path: Path) -> None:
    mgr = TaskManager(workspace=tmp_path)

    t_low = asyncio.run(mgr.create_task("Low priority task", priority=TaskPriority.LOW))
    t_crit = asyncio.run(mgr.create_task("Critical priority task", priority=TaskPriority.CRITICAL))
    t_med = asyncio.run(mgr.create_task("Medium priority task", priority=TaskPriority.MEDIUM))

    asyncio.run(mgr.queue_task(t_low.id))
    asyncio.run(mgr.queue_task(t_crit.id))
    asyncio.run(mgr.queue_task(t_med.id))

    tasks = mgr.list_tasks()
    assert [t.id for t in tasks] == [t_crit.id, t_med.id, t_low.id]


# ---------------------------------------------------------------------------
# 3. Lifecycle State Transitions (Pause, Resume, Cancel, Complete)
# ---------------------------------------------------------------------------


def test_lifecycle_transitions(tmp_path: Path) -> None:
    mgr = TaskManager(workspace=tmp_path)

    task = asyncio.run(mgr.create_task("Test lifecycle"))
    assert task.status == TaskStatus.PENDING

    # Queue
    task = asyncio.run(mgr.queue_task(task.id))
    assert task.status == TaskStatus.QUEUED

    # Start
    task = asyncio.run(mgr.start_task(task.id))
    assert task.status == TaskStatus.RUNNING

    # Update Progress
    task = asyncio.run(mgr.update_progress(task.id, 50.0, "Halfway done"))
    assert task.progress == 50.0

    # Pause
    task = asyncio.run(mgr.pause_task(task.id, "Waiting for user input"))
    assert task.status == TaskStatus.PAUSED

    # Resume
    task = asyncio.run(mgr.resume_task(task.id))
    assert task.status == TaskStatus.QUEUED

    # Complete
    task = asyncio.run(mgr.complete_task(task.id, "Successfully finished"))
    assert task.status == TaskStatus.COMPLETED
    assert task.progress == 100.0
    assert task.result == "Successfully finished"


def test_task_cancellation(tmp_path: Path) -> None:
    mgr = TaskManager(workspace=tmp_path)
    task = asyncio.run(mgr.create_task("Cancel test"))
    asyncio.run(mgr.queue_task(task.id))

    cancelled = asyncio.run(mgr.cancel_task(task.id, reason="No longer needed"))
    assert cancelled.status == TaskStatus.CANCELLED
    assert any("No longer needed" in rec.detail for rec in cancelled.history)


# ---------------------------------------------------------------------------
# 4. Checkpoints Creation & Restoration
# ---------------------------------------------------------------------------


def test_checkpoint_saving_and_restoration(tmp_path: Path) -> None:
    mgr = TaskManager(workspace=tmp_path)
    task = asyncio.run(mgr.create_task("Checkpoint test"))

    cp1 = asyncio.run(mgr.create_checkpoint(task.id, step_index=1, state_data={"var": "value1"}))
    cp2 = asyncio.run(mgr.create_checkpoint(task.id, step_index=2, state_data={"var": "value2"}))

    restored_latest = asyncio.run(mgr.restore_checkpoint(task.id))
    assert restored_latest.checkpoint_id == cp2.checkpoint_id
    assert restored_latest.state_data["var"] == "value2"

    restored_specific = asyncio.run(mgr.restore_checkpoint(task.id, cp1.checkpoint_id))
    assert restored_specific.checkpoint_id == cp1.checkpoint_id
    assert restored_specific.state_data["var"] == "value1"


# ---------------------------------------------------------------------------
# 5. File Persistence Across Sessions
# ---------------------------------------------------------------------------


def test_persistence_across_store(tmp_path: Path) -> None:
    # Session 1: Create and complete tasks
    mgr1 = TaskManager(workspace=tmp_path)
    t1 = asyncio.run(mgr1.create_task("Persisted Task 1", priority=TaskPriority.HIGH))
    asyncio.run(mgr1.create_checkpoint(t1.id, 1, {"step": "init"}))
    asyncio.run(mgr1.complete_task(t1.id, "Done"))

    # Session 2: Reload from disk
    mgr2 = TaskManager(workspace=tmp_path)
    reloaded_t1 = mgr2.get_task(t1.id)

    assert reloaded_t1 is not None
    assert reloaded_t1.title == "Persisted Task 1"
    assert reloaded_t1.status == TaskStatus.COMPLETED
    assert len(reloaded_t1.checkpoints) == 1


# ---------------------------------------------------------------------------
# 6. Async Event Bus Integration
# ---------------------------------------------------------------------------


def test_event_bus_emission(tmp_path: Path) -> None:
    event_bus = TaskEventBus()
    received_events: list[TaskEvent] = []

    def _listener(evt: TaskEvent) -> None:
        received_events.append(evt)

    event_bus.subscribe(_listener)
    mgr = TaskManager(workspace=tmp_path, event_bus=event_bus)

    task = asyncio.run(mgr.create_task("Event bus test"))
    asyncio.run(mgr.queue_task(task.id))
    asyncio.run(mgr.cancel_task(task.id))

    assert len(received_events) >= 3
    event_types = [e.event_type for e in received_events]
    assert "task_created" in event_types
    assert "task_queued" in event_types
    assert "task_cancelled" in event_types


# ---------------------------------------------------------------------------
# 7. Unresolved Dependencies Guard
# ---------------------------------------------------------------------------


def test_unresolved_dependencies_guard(tmp_path: Path) -> None:
    mgr = TaskManager(workspace=tmp_path)
    t1 = asyncio.run(mgr.create_task("Parent Task"))
    t2 = asyncio.run(mgr.create_task("Child Task", depends_on=[t1.id]))

    with pytest.raises(RuntimeError, match="unresolved dependencies"):
        asyncio.run(mgr.start_task(t2.id))

    # Complete t1, then t2 can start
    asyncio.run(mgr.start_task(t1.id))
    asyncio.run(mgr.complete_task(t1.id, "Parent done"))

    started_t2 = asyncio.run(mgr.start_task(t2.id))
    assert started_t2.status == TaskStatus.RUNNING


# ---------------------------------------------------------------------------
# 8. TaskTool Integration
# ---------------------------------------------------------------------------


def test_task_tool_integration(tmp_path: Path) -> None:
    from pulse.tools import TaskTool

    mgr = TaskManager(workspace=tmp_path)
    tool = TaskTool(mgr)

    t1 = asyncio.run(mgr.create_task("CLI Task Test"))

    # Execute "tasks" tool call
    res_list = asyncio.run(tool.execute(ToolInvocation(name="tasks")))
    assert t1.id in res_list.content

    # Execute "task" tool call (get details)
    res_detail = asyncio.run(tool.execute(ToolInvocation(name="task", arguments={"id": t1.id})))
    assert "CLI Task Test" in res_detail.content

    # Execute "cancel" tool call
    res_cancel = asyncio.run(tool.execute(ToolInvocation(name="cancel", arguments={"id": t1.id, "reason": "Test cancel"})))
    assert "CANCELLED" in res_cancel.content
