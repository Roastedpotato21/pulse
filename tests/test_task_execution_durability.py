"""Durability contracts for task lifecycle events and remote attempts."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from pulse.execution import RemoteTaskExecutor
from pulse.sandbox.process import ProcessResult
from pulse.task_manager import (
    RetryDeferredError,
    StaleWorkerError,
    TaskManager,
    TaskStatus,
    TaskStore,
)


def test_task_outbox_is_ordered_durable_and_acknowledged(tmp_path: Path) -> None:
    """Lifecycle observations survive manager recreation and ack idempotently."""

    async def run() -> tuple[str, list[str]]:
        manager = TaskManager(tmp_path)
        task = await manager.create_task("durable events")
        await manager.queue_task(task.id)
        await manager.start_task(task.id)
        await manager.update_progress(task.id, 25, "started work")

        events = manager.store.pending_outbox_events()
        assert [event.sequence for event in events] == list(
            range(1, len(events) + 1)
        )
        assert [event.event_type for event in events] == [
            "task_created",
            "task_queued",
            "task_started",
            "task_progress",
        ]
        return events[0].event_id, [event.event_id for event in events]

    first_event_id, all_event_ids = asyncio.run(run())
    restarted_store = TaskStore(tmp_path)
    assert [event.event_id for event in restarted_store.pending_outbox_events()] == all_event_ids
    assert restarted_store.acknowledge_outbox_event(first_event_id) is True
    assert restarted_store.acknowledge_outbox_event(first_event_id) is False
    assert first_event_id not in {
        event.event_id for event in restarted_store.pending_outbox_events()
    }


def test_remote_execution_binding_is_created_before_dispatch_and_rotates_on_retry(
    tmp_path: Path,
) -> None:
    """Each remote lease attempt receives one durable, fenced external ID."""

    async def run() -> None:
        manager = TaskManager(tmp_path, heartbeat_interval=0.1, lease_duration=1.0)
        task = await manager.create_task(
            "remote task", metadata={"execution_mode": "remote"}
        )
        await manager.queue_task(task.id)
        first_attempt = await manager.start_task(task.id)
        first_id = await manager.remote_execution_id_for_task(task.id)

        assert first_id == f"{task.id}-attempt-{first_attempt.lease_epoch}"
        assert first_attempt.remote_execution_id == first_id

        await manager.fail_task(task.id, "transient remote failure")
        retry = manager.get_task(task.id)
        assert retry.status == TaskStatus.QUEUED
        # Advance the durable retry clock; this test concerns attempt identity,
        # while backoff behavior is covered independently below.
        retry.next_retry_at = "2000-01-01T00:00:00+00:00"
        manager.store.update_task(retry)
        retry.version += 1
        second_attempt = await manager.start_task(task.id)
        second_id = await manager.remote_execution_id_for_task(task.id)

        assert second_id == f"{task.id}-attempt-{second_attempt.lease_epoch}"
        assert second_id != first_id
        started_events = [
            event
            for event in manager.store.pending_outbox_events()
            if event.event_type == "task_started"
        ]
        assert [event.payload["remote_execution_id"] for event in started_events] == [
            first_id,
            second_id,
        ]

    asyncio.run(run())


def test_remote_execution_binding_requires_current_owner(tmp_path: Path) -> None:
    """A non-owner cannot obtain an external execution capability."""

    async def run() -> None:
        owner = TaskManager(tmp_path, heartbeat_interval=0.1, lease_duration=1.0)
        task = await owner.create_task(
            "remote task", metadata={"execution_mode": "remote"}
        )
        await owner.queue_task(task.id)
        await owner.start_task(task.id)

        intruder = TaskManager(tmp_path, store=TaskStore(tmp_path))
        with pytest.raises(StaleWorkerError, match="no longer owns"):
            await intruder.remote_execution_id_for_task(task.id)

    asyncio.run(run())


def test_retry_backoff_is_durable_and_exhaustion_dead_letters(tmp_path: Path) -> None:
    """Retries cannot hot-loop and exhausted work requires manual action."""

    async def run() -> None:
        manager = TaskManager(tmp_path, heartbeat_interval=0.1, lease_duration=1.0)
        task = await manager.create_task("retry budget", max_retries=1)
        await manager.queue_task(task.id)
        await manager.start_task(task.id)
        retry = await manager.fail_task(task.id, "temporary failure")

        assert retry.status == TaskStatus.QUEUED
        assert retry.next_retry_at is not None
        assert retry.history[-1].action == "retry_scheduled"
        with pytest.raises(RetryDeferredError):
            await manager.start_task(task.id)
        assert await manager.process_queue(lambda _task: asyncio.sleep(0)) == []

        retry.next_retry_at = "2000-01-01T00:00:00+00:00"
        manager.store.update_task(retry)
        retry.version += 1
        await manager.start_task(task.id)
        dead_letter = await manager.fail_task(
            task.id, "retry budget exhausted"
        )

        assert dead_letter.status == TaskStatus.DEAD_LETTER
        assert dead_letter.next_retry_at is None
        assert dead_letter.history[-1].action == "dead_lettered"
        assert any(
            event.event_type == "task_dead_lettered"
            for event in manager.store.pending_outbox_events()
        )

        resumed = await manager.resume_task(task.id)
        assert resumed.status == TaskStatus.QUEUED
        assert resumed.next_retry_at is None

    asyncio.run(run())


def test_remote_task_executor_passes_fenced_binding_to_sandbox(tmp_path: Path) -> None:
    """The dispatch bridge submits precisely the ID that recovery persists."""

    class FakeRemoteSandbox:
        def __init__(self) -> None:
            self.backend = SimpleNamespace(name="remote")
            self.execution_ids: list[str | None] = []

        async def initialize(self) -> None:
            return None

        async def execute_command(
            self, _command: str | list[str], **kwargs: object
        ) -> ProcessResult:
            self.execution_ids.append(kwargs.get("execution_id"))
            return ProcessResult(
                command="echo done",
                exit_code=0,
                stdout="done",
                stderr="",
                duration_ms=1.0,
            )

    async def run() -> None:
        manager = TaskManager(tmp_path, heartbeat_interval=0.1, lease_duration=1.0)
        task = await manager.create_task(
            "dispatch remotely", metadata={"execution_mode": "remote"}
        )
        await manager.queue_task(task.id)
        sandbox = FakeRemoteSandbox()
        executor = RemoteTaskExecutor(manager, sandbox)  # type: ignore[arg-type]

        completed = await executor.execute(task.id, ["echo", "done"])
        expected_id = f"{task.id}-attempt-1"

        assert sandbox.execution_ids == [expected_id]
        assert completed.remote_execution_id == expected_id
        assert completed.status == TaskStatus.COMPLETED

    asyncio.run(run())
