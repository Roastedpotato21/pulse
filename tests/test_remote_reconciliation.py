"""Protocol and workflow recovery tests for remote sandbox reconciliation."""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

import pytest

from pulse.sandbox.remote.client import RemoteClient
from pulse.sandbox.remote.models import ExecutionResultModel
from pulse.sandbox.remote.server import RemoteExecutionStore, RemoteServer
from pulse.task_manager import TaskManager, TaskStatus


@pytest.fixture
async def reconciliation_server(tmp_path: Path):
    server = RemoteServer(port=0, auth_token="reconcile-token")
    server.store = RemoteExecutionStore(tmp_path / "executions.sqlite3")
    task = asyncio.create_task(server.start())
    try:
        await server.wait_until_ready()
        yield server, server.port
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


def _tenant(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()[:12]


def _result(execution_id: str) -> dict[str, object]:
    return ExecutionResultModel(
        execution_id=execution_id,
        command="echo recovered",
        exit_code=0,
        stdout="recovered",
        stderr="",
        duration_ms=1.0,
    ).to_dict()


@pytest.mark.asyncio
async def test_remote_status_and_completed_attach_are_execution_scoped(
    reconciliation_server,
) -> None:
    server, port = reconciliation_server
    execution_id = "remote-attempt-1"
    server.store.create(execution_id, _tenant("reconcile-token"))
    server.store.update(execution_id, "COMPLETED", _result(execution_id))

    client = RemoteClient(f"ws://127.0.0.1:{port}", "reconcile-token")
    assert await client.status(execution_id) == "COMPLETED"
    attached = await asyncio.wait_for(client.attach(execution_id), timeout=1.0)
    assert attached.execution_id == execution_id
    assert attached.stdout == "recovered"
    await client.disconnect()
    assert client._listener_task is None


@pytest.mark.asyncio
async def test_remote_attach_missing_execution_fails_without_hanging(
    reconciliation_server,
) -> None:
    _, port = reconciliation_server
    client = RemoteClient(f"ws://127.0.0.1:{port}", "reconcile-token")
    with pytest.raises(RuntimeError, match="NOT_FOUND"):
        await asyncio.wait_for(client.attach("missing-execution"), timeout=1.0)
    await client.disconnect()
    assert client._listener_task is None


@pytest.mark.asyncio
async def test_remote_attach_unknown_execution_fails_without_hanging(
    reconciliation_server,
) -> None:
    server, port = reconciliation_server
    execution_id = "interrupted-attempt"
    server.store.create(execution_id, _tenant("reconcile-token"))
    server.store.mark_interrupted_running_unknown()

    client = RemoteClient(f"ws://127.0.0.1:{port}", "reconcile-token")
    with pytest.raises(RuntimeError, match="UNKNOWN"):
        await asyncio.wait_for(client.attach(execution_id), timeout=1.0)
    await client.disconnect()
    assert client._listener_task is None


@pytest.mark.asyncio
async def test_task_recovery_uses_persisted_remote_execution_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FakeRemoteClient:
        requested_execution_id: str | None = None

        def __init__(self, *_args: object) -> None:
            pass

        async def status(self, execution_id: str) -> str:
            type(self).requested_execution_id = execution_id
            return "COMPLETED"

        async def attach(self, execution_id: str) -> ExecutionResultModel:
            return ExecutionResultModel.from_dict(_result(execution_id))

        async def disconnect(self) -> None:
            pass

    monkeypatch.setenv("PULSE_REMOTE_URL", "ws://127.0.0.1:8080")
    monkeypatch.setenv("PULSE_REMOTE_TOKEN", "reconcile-token")
    monkeypatch.setattr("pulse.sandbox.remote.client.RemoteClient", FakeRemoteClient)

    manager = TaskManager(tmp_path, heartbeat_interval=0.1, lease_duration=1.0)
    task = await manager.create_task(
        "recover remote",
        metadata={
            "execution_mode": "remote",
            "remote_execution_id": "remote-attempt-42",
        },
    )
    await manager.queue_task(task.id)
    running = await manager.start_task(task.id)
    running.lease_expires_at = "2000-01-01T00:00:00+00:00"
    manager.store.update_task(
        running,
        expected_status=TaskStatus.RUNNING,
        expected_owner_id=manager.worker_id,
        expected_lease_epoch=running.lease_epoch,
    )
    running.version += 1

    await manager.recover_tasks()
    recovered = manager.get_task(task.id)
    assert FakeRemoteClient.requested_execution_id == "remote-attempt-42"
    assert recovered is not None and recovered.status == TaskStatus.COMPLETED
