import asyncio
import subprocess
import sys
import time
from pathlib import Path

import pytest

from pulse.task_manager import TaskConcurrencyError, TaskManager, TaskStore

# Scenario A: Progress conflict. 
# Covered partially, but we will write a script to simulate a direct caller to TaskManager APIs.

def test_taskstore_occ_progress_conflict_retry(tmp_path: Path):
    """Verify automatic retry inside TaskManager when conflict occurs (Scenario A)."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    
    store = TaskStore(workspace)
    tm = TaskManager(workspace, store=store)
    
    task_id = None
    async def create_initial():
        nonlocal task_id
        t = await tm.create_task("Retry Test")
        task_id = t.id
    asyncio.run(create_initial())

    # We will spawn a subprocess that updates progress. 
    # Because we want to test TaskManager's retry natively, the subprocess just calls `update_progress` once.
    # We will simulate a conflict by having the MAIN process update the task BEFORE the subprocess calls update_progress.
    
    script = f"""
import sys
import asyncio
from pathlib import Path
sys.path.insert(0, r"{Path(__file__).parent.parent.absolute() / 'src'}")
from pulse.task_manager import TaskManager, TaskStore, TaskConcurrencyError

async def run():
    workspace = Path(r"{workspace}")
    store = TaskStore(workspace)
    tm = TaskManager(workspace, store=store)
    
    # Get task to cache it
    task = tm.get_task("{task_id}")
    
    # Wait for main process to update it
    await asyncio.sleep(1)
    
    # Call update_progress. It should experience a conflict, but transparently RETRY!
    await tm.update_progress("{task_id}", 90.0, "Updated by worker")

if __name__ == "__main__":
    asyncio.run(run())
"""
    script_path = tmp_path / "worker.py"
    script_path.write_text(script)
    
    p = subprocess.Popen([sys.executable, str(script_path)])
    
    time.sleep(0.2) # Let worker start and cache version 1
    
    # Main process updates to version 2
    async def update_main():
        await tm.update_progress(task_id, 20.0, "Updated by main")
    asyncio.run(update_main())
    
    p.wait()
    assert p.returncode == 0
    
    # Reload and verify BOTH updates happened semantically!
    # Worker requested 90.0. Main requested 20.0.
    # Because worker slept 1 second, it should have overwritten progress to 90.0, 
    # but the VERSION should be 3 (1 -> 2 by main, 2 -> 3 by worker retry).
    final_task = TaskManager(workspace).get_task(task_id)
    assert final_task.progress == 90.0
    assert final_task.version == 3

def test_taskstore_occ_status_vs_progress(tmp_path: Path):
    """Scenario B: Status vs progress conflict."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    
    store = TaskStore(workspace)
    tm = TaskManager(workspace, store=store)
    
    task_id = None
    async def create_initial():
        nonlocal task_id
        t = await tm.create_task("Conflict Test")
        task_id = t.id
    asyncio.run(create_initial())

    script = f"""
import sys
import asyncio
from pathlib import Path
sys.path.insert(0, r"{Path(__file__).parent.parent.absolute() / 'src'}")
from pulse.task_manager import TaskManager, TaskStore

async def run():
    workspace = Path(r"{workspace}")
    tm = TaskManager(workspace)
    tm.get_task("{task_id}")
    await asyncio.sleep(1)
    await tm.complete_task("{task_id}")

if __name__ == "__main__":
    asyncio.run(run())
"""
    script_path = tmp_path / "worker.py"
    script_path.write_text(script)
    
    p = subprocess.Popen([sys.executable, str(script_path)])
    
    import time
    time.sleep(0.2)
    
    async def update_main():
        await tm.update_progress(task_id, 50.0)
    asyncio.run(update_main())
    
    p.wait()
    assert p.returncode == 0
    
    final_task = TaskManager(workspace).get_task(task_id)
    # complete_task overwrites progress to 100.0 if not specified otherwise
    assert final_task.progress == 100.0
    assert final_task.status.value == "COMPLETED"
    assert final_task.version == 3

def test_taskstore_occ_checkpoint_vs_status(tmp_path: Path):
    """Scenario C: Checkpoint vs status conflict."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    
    store = TaskStore(workspace)
    tm = TaskManager(workspace, store=store)
    tm.worker_id = "test-worker"
    
    task_id = None
    async def create_initial():
        nonlocal task_id
        t = await tm.create_task("Checkpoint Test")
        task_id = t.id
    asyncio.run(create_initial())

    script = f"""
import sys
import asyncio
from pathlib import Path
sys.path.insert(0, r"{Path(__file__).parent.parent.absolute() / 'src'}")
from pulse.task_manager import TaskManager, TaskStore

async def run():
    workspace = Path(r"{workspace}")
    tm = TaskManager(workspace)
    tm.worker_id = "test-worker"
    tm.get_task("{task_id}")
    await asyncio.sleep(1)
    await tm.create_checkpoint("{task_id}", "chk1", {{}})

if __name__ == "__main__":
    asyncio.run(run())
"""
    script_path = tmp_path / "worker.py"
    script_path.write_text(script)
    
    p = subprocess.Popen([sys.executable, str(script_path)])
    import time
    time.sleep(0.2)
    
    async def update_main():
        await tm.start_task(task_id)
    asyncio.run(update_main())
    
    p.wait()
    # A fresh manager without the acquired lease epoch cannot checkpoint a
    # RUNNING task, even when it uses the same worker-id string.
    assert p.returncode != 0
    
    final_task = TaskManager(workspace).get_task(task_id)
    assert final_task.status.value == "RUNNING"
    assert len(final_task.checkpoints) == 0
    assert final_task.version == 2

def test_taskstore_occ_retry_limit(tmp_path: Path):
    """Scenario D: Retry limit forces TaskConcurrencyError."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    
    store = TaskStore(workspace)
    tm = TaskManager(workspace, store=store)
    
    task_id = None
    async def create_initial():
        nonlocal task_id
        t = await tm.create_task("Limit Test")
        task_id = t.id
    asyncio.run(create_initial())

    # We will mock update_task to always raise TaskConcurrencyError
    async def run_fail():
        from unittest.mock import patch
        with patch.object(store, "update_task", side_effect=TaskConcurrencyError(task_id)), pytest.raises(TaskConcurrencyError):
                await tm.update_progress(task_id, 99.0)
                
    asyncio.run(run_fail())

def test_taskstore_cannot_steal_healthy_lease(tmp_path: Path):
    """Test that a second worker cannot steal a lease that hasn't expired."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    
    store = TaskStore(workspace)
    tm1 = TaskManager(workspace, store=store, heartbeat_interval=0.5, lease_duration=5.0)
    
    async def run():
        task = await tm1.create_task("Test Steal")
        await tm1.queue_task(task.id)
        
        # Create tm2 AFTER task is in DB
        tm2 = TaskManager(workspace, store=TaskStore(workspace), heartbeat_interval=0.5, lease_duration=5.0)
        
        # Worker 1 acquires
        await tm1.start_task(task.id)
        
        # Worker 2 attempts to acquire and should fail with RuntimeError
        with pytest.raises(RuntimeError, match="is currently owned and lease is active"):
            await tm2.start_task(task.id)
            
    asyncio.run(run())

def test_taskstore_stale_worker_rejected(tmp_path: Path):
    """Test that a worker whose lease was stolen cannot perform terminal operations or heartbeats."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    
    from pulse.task_manager import StaleWorkerError, TaskStatus
    
    store = TaskStore(workspace)
    tm1 = TaskManager(workspace, store=store, heartbeat_interval=0.1, lease_duration=0.2)
    
    async def run():
        task = await tm1.create_task("Test Stale")
        await tm1.queue_task(task.id)
        
        # Create tm2 AFTER task is in DB
        tm2 = TaskManager(workspace, store=TaskStore(workspace), heartbeat_interval=0.1, lease_duration=5.0)
        
        # Worker 1 acquires
        await tm1.start_task(task.id)
        
        # Wait for lease to expire without heartbeat (by stopping it manually or just waiting past duration)
        tm1._stop_heartbeat(task.id)
        await asyncio.sleep(0.3)
        
        # Worker 2 recovers the task
        await tm2.recover_tasks()
        task2 = tm2.get_task(task.id)
        assert task2.status == TaskStatus.QUEUED
        
        # Worker 2 acquires it
        await tm2.start_task(task.id)
        
        # Worker 1 wakes up and tries to complete it
        with pytest.raises(StaleWorkerError):
            await tm1.complete_task(task.id, "Done by tm1")
            
        # Worker 1 wakes up and tries to renew lease
        with pytest.raises(StaleWorkerError):
            await tm1.renew_lease(task.id)
            
    asyncio.run(run())

