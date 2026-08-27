"""Adversarial tests for the GAP-02 P0 remediation: lease-bound execution.

Tests prove that:
1. Losing a lease cancels worker_func execution.
2. A recovered worker cannot run concurrently with a stale worker.
3. Healthy workers are never cancelled unnecessarily.
4. Worker failure cleans up the heartbeat.
5. Cancelling process_queue cancels both worker and heartbeat.
6. The completion/lease-loss race is handled safely.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from pulse.task_manager import (
    StaleWorkerError,
    TaskManager,
    TaskStatus,
    TaskStore,
)

# ---------------------------------------------------------------------------
# TEST 1 — Lease loss cancels worker
# ---------------------------------------------------------------------------


def test_lease_loss_cancels_worker(tmp_path: Path) -> None:
    """When the heartbeat detects ownership loss, worker_func must be
    cancelled and process_queue must NOT mark the task COMPLETED."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    execution_started = asyncio.Event()
    execution_finished = asyncio.Event()

    async def long_running_worker(task):
        execution_started.set()
        await asyncio.sleep(60)  # Would never finish naturally
        execution_finished.set()
        return "Should never reach here"

    tm = TaskManager(
        workspace,
        heartbeat_interval=0.1,
        lease_duration=0.3,
    )

    async def run():
        t = await tm.create_task("Lease Loss Test")
        await tm.queue_task(t.id)

        # Start processing — will acquire lease, start worker + heartbeat
        pq_task = asyncio.create_task(tm.process_queue(long_running_worker))

        # Wait until the worker is actually running
        await execution_started.wait()

        # Now simulate ownership loss: stop the heartbeat and manually
        # expire the lease, then steal ownership via a second manager.
        tm._stop_heartbeat(t.id)
        # Force the in-memory lease to be expired
        task_obj = tm.get_task(t.id)
        task_obj.lease_expires_at = "2000-01-01T00:00:00+00:00"
        tm.store.update_task(
            task_obj,
            expected_status=TaskStatus.RUNNING,
            expected_owner_id=tm.worker_id,
            expected_lease_epoch=task_obj.lease_epoch,
        )
        task_obj.version += 1

        # A second manager recovers the task
        tm2 = TaskManager(
            workspace,
            store=TaskStore(workspace),
            heartbeat_interval=0.1,
            lease_duration=5.0,
        )
        await tm2.recover_tasks()

        # Now restart the heartbeat for tm with stale ownership —
        # it will discover it's been stolen and raise LeaseLostError.
        tm._heartbeat_tasks[t.id] = asyncio.create_task(
            tm._heartbeat_loop(t.id)
        )

        # Wait for process_queue to finish handling the lease loss
        result = await asyncio.wait_for(pq_task, timeout=5.0)

        # Verify: worker was cancelled (execution_finished NOT set)
        assert not execution_finished.is_set()

        # Verify: process_queue did NOT mark it completed
        assert len(result) == 0  # No tasks in the processed list

        # Verify: the task is now owned by tm2 (recovered to QUEUED)
        task_final = tm2.get_task(t.id)
        assert task_final.status == TaskStatus.QUEUED
        assert task_final.owner_id is None

    asyncio.run(run())


# ---------------------------------------------------------------------------
# TEST 2 — Stale worker cannot continue after recovery
# ---------------------------------------------------------------------------


def test_stale_worker_cannot_continue_after_recovery(tmp_path: Path) -> None:
    """Worker A acquires a task, then its lease expires.  Worker B recovers
    and acquires it.  Worker A's worker_func must be cancelled, and Worker A
    cannot subsequently complete/fail/mutate the task."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    worker_a_started = asyncio.Event()
    worker_a_finished = asyncio.Event()

    async def worker_a_func(task):
        worker_a_started.set()
        await asyncio.sleep(60)
        worker_a_finished.set()
        return "Worker A result"

    tm_a = TaskManager(
        workspace,
        heartbeat_interval=0.1,
        lease_duration=0.3,
    )

    async def run():
        t = await tm_a.create_task("Stale Worker Test")
        await tm_a.queue_task(t.id)

        # Start Worker A's process_queue
        pq_a = asyncio.create_task(tm_a.process_queue(worker_a_func))

        await worker_a_started.wait()

        # Stop heartbeat and expire lease manually
        tm_a._stop_heartbeat(t.id)
        task_obj = tm_a.get_task(t.id)
        task_obj.lease_expires_at = "2000-01-01T00:00:00+00:00"
        tm_a.store.update_task(
            task_obj,
            expected_status=TaskStatus.RUNNING,
            expected_owner_id=tm_a.worker_id,
            expected_lease_epoch=task_obj.lease_epoch,
        )
        task_obj.version += 1

        # Worker B recovers
        tm_b = TaskManager(
            workspace,
            store=TaskStore(workspace),
            heartbeat_interval=0.1,
            lease_duration=5.0,
        )
        await tm_b.recover_tasks()

        # Restart heartbeat with stale ownership to trigger LeaseLostError
        tm_a._heartbeat_tasks[t.id] = asyncio.create_task(
            tm_a._heartbeat_loop(t.id)
        )

        # Wait for Worker A's process_queue to finish
        await asyncio.wait_for(pq_a, timeout=5.0)

        # Worker A's execution was cancelled
        assert not worker_a_finished.is_set()

        # Worker A cannot mutate the task
        with pytest.raises(StaleWorkerError):
            await tm_a.complete_task(t.id, "Worker A late completion")

        # Worker B can acquire and complete
        await tm_b.start_task(t.id)
        completed = await tm_b.complete_task(t.id, "Worker B result")
        assert completed.status == TaskStatus.COMPLETED
        assert completed.result == "Worker B result"

    asyncio.run(run())


# ---------------------------------------------------------------------------
# TEST 3 — Healthy heartbeat does NOT cancel execution
# ---------------------------------------------------------------------------


def test_healthy_heartbeat_does_not_cancel_execution(tmp_path: Path) -> None:
    """A task with a healthy heartbeat should complete normally without
    being cancelled.  Multiple heartbeat intervals must pass during
    execution."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    heartbeat_count = 0

    tm = TaskManager(
        workspace,
        heartbeat_interval=0.05,
        lease_duration=0.5,
    )

    # Patch renew_lease to count heartbeats
    original_renew = tm.renew_lease

    async def counting_renew(task_id):
        nonlocal heartbeat_count
        await original_renew(task_id)
        heartbeat_count += 1

    tm.renew_lease = counting_renew

    async def normal_worker(task):
        # Sleep long enough for multiple heartbeats to fire
        await asyncio.sleep(0.3)
        return "Normal completion"

    async def run():
        t = await tm.create_task("Healthy Heartbeat Test")
        await tm.queue_task(t.id)

        result = await tm.process_queue(normal_worker)

        assert len(result) == 1
        assert result[0].status == TaskStatus.COMPLETED
        assert result[0].result == "Normal completion"
        assert heartbeat_count >= 2  # At least 2 heartbeats during 0.3s / 0.05s
        assert t.id not in tm._heartbeat_tasks  # Heartbeat cleaned up

    asyncio.run(run())


# ---------------------------------------------------------------------------
# TEST 4 — Worker failure cleans up heartbeat
# ---------------------------------------------------------------------------


def test_worker_failure_cleans_up_heartbeat(tmp_path: Path) -> None:
    """If worker_func raises an exception, the heartbeat must be stopped
    and the task must enter the failure/retry lifecycle."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    tm = TaskManager(
        workspace,
        heartbeat_interval=0.05,
        lease_duration=1.0,
    )

    async def failing_worker(task):
        await asyncio.sleep(0.1)
        raise RuntimeError("Deliberate worker failure")

    async def run():
        t = await tm.create_task("Worker Failure Test")
        await tm.queue_task(t.id)

        result = await tm.process_queue(failing_worker)

        assert len(result) == 1
        # Should be QUEUED (retry) since retries < max_retries
        assert result[0].status == TaskStatus.QUEUED
        assert result[0].retries == 1
        assert "Deliberate worker failure" in (result[0].error or "")

        # Heartbeat must be cleaned up
        assert t.id not in tm._heartbeat_tasks

        # No leaked asyncio tasks
        # (If heartbeat leaked, it would cause "Task was destroyed" warnings)

    asyncio.run(run())


# ---------------------------------------------------------------------------
# TEST 5 — Process queue cancellation
# ---------------------------------------------------------------------------


def test_process_queue_cancellation(tmp_path: Path) -> None:
    """Cancelling process_queue while worker_func is running must cancel
    both the worker and heartbeat.  No task should be incorrectly marked
    COMPLETED."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    worker_started = asyncio.Event()
    worker_cancelled = asyncio.Event()

    async def cancellable_worker(task):
        worker_started.set()
        try:
            await asyncio.sleep(60)
            return "Should not reach"
        except asyncio.CancelledError:
            worker_cancelled.set()
            raise

    tm = TaskManager(
        workspace,
        heartbeat_interval=0.05,
        lease_duration=1.0,
    )

    async def run():
        t = await tm.create_task("Cancel PQ Test")
        await tm.queue_task(t.id)

        pq_task = asyncio.create_task(tm.process_queue(cancellable_worker))

        await worker_started.wait()

        # Cancel process_queue from outside
        pq_task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await pq_task

        # Worker was cancelled
        assert worker_cancelled.is_set()

        # Task must NOT be COMPLETED
        task_final = tm.get_task(t.id)
        assert task_final.status != TaskStatus.COMPLETED

        # Heartbeat must be cleaned up
        assert t.id not in tm._heartbeat_tasks

    asyncio.run(run())


# ---------------------------------------------------------------------------
# TEST 6 — Completion / lease-loss race
# ---------------------------------------------------------------------------


def test_completion_lease_loss_race(tmp_path: Path) -> None:
    """When worker completion and lease loss happen near-simultaneously,
    the system must use OCC/ownership to determine the outcome:
    - If ownership is valid at complete_task time: COMPLETED is correct.
    - If ownership is lost: StaleWorkerError prevents false completion."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    tm = TaskManager(
        workspace,
        heartbeat_interval=0.05,
        lease_duration=1.0,
    )

    async def run():
        t = await tm.create_task("Race Test")
        await tm.queue_task(t.id)
        await tm.start_task(t.id)

        # Simulate: worker has just finished, but before complete_task
        # is called, another manager steals ownership.

        # Expire the lease
        task_obj = tm.get_task(t.id)
        task_obj.lease_expires_at = "2000-01-01T00:00:00+00:00"
        tm.store.update_task(
            task_obj,
            expected_status=TaskStatus.RUNNING,
            expected_owner_id=tm.worker_id,
            expected_lease_epoch=task_obj.lease_epoch,
        )
        task_obj.version += 1

        # Second manager recovers and acquires
        tm2 = TaskManager(
            workspace,
            store=TaskStore(workspace),
            heartbeat_interval=0.05,
            lease_duration=5.0,
        )
        await tm2.recover_tasks()
        await tm2.start_task(t.id)

        # Now tm (stale) tries to complete — must be rejected
        with pytest.raises(StaleWorkerError):
            await tm.complete_task(t.id, "Stale completion")

        # tm2 (valid owner) can complete
        completed = await tm2.complete_task(t.id, "Valid completion")
        assert completed.status == TaskStatus.COMPLETED
        assert completed.result == "Valid completion"

    asyncio.run(run())


def test_fresh_non_owner_cannot_mutate_running_task(tmp_path: Path) -> None:
    """A manager that did not acquire the lease cannot use current OCC state
    to mutate another worker's RUNNING task."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    async def run():
        owner = TaskManager(workspace, heartbeat_interval=0.1, lease_duration=1.0)
        task = await owner.create_task("Durable fence")
        await owner.queue_task(task.id)
        await owner.start_task(task.id)

        intruder = TaskManager(workspace, store=TaskStore(workspace))
        with pytest.raises(StaleWorkerError):
            await intruder.complete_task(task.id, "unauthorized")
        with pytest.raises(StaleWorkerError):
            await intruder.update_progress(task.id, 50.0)

    asyncio.run(run())


def test_worker_that_ignores_cancellation_is_fenced(tmp_path: Path) -> None:
    """Cancellation refusal is bounded and the old epoch cannot mutate."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    started = asyncio.Event()
    cancellation_seen = asyncio.Event()
    release = asyncio.Event()

    async def stubborn_worker(task):
        started.set()
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            cancellation_seen.set()
            await release.wait()
            return "stale result"

    async def run():
        owner = TaskManager(workspace, heartbeat_interval=0.05, lease_duration=0.2)
        task = await owner.create_task("Cancellation fence")
        await owner.queue_task(task.id)
        queue_task = asyncio.create_task(owner.process_queue(stubborn_worker))
        await started.wait()

        # Fence the old epoch through normal recovery.
        current = owner.get_task(task.id)
        current.lease_expires_at = "2000-01-01T00:00:00+00:00"
        owner.store.update_task(
            current,
            expected_status=TaskStatus.RUNNING,
            expected_owner_id=owner.worker_id,
            expected_lease_epoch=current.lease_epoch,
        )
        current.version += 1
        recovered = TaskManager(workspace, store=TaskStore(workspace))
        await recovered.recover_tasks()

        await asyncio.sleep(0.1)
        assert cancellation_seen.is_set()
        with pytest.raises(StaleWorkerError):
            await owner.update_progress(task.id, 99.0)

        release.set()
        await asyncio.wait_for(queue_task, timeout=3.0)
        assert recovered.get_task(task.id).status == TaskStatus.QUEUED

    asyncio.run(run())
