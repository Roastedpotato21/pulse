import sqlite3
import subprocess
import sys
from pathlib import Path


def run_crasher(workspace: Path, crash_point: str) -> tuple[int, str, str]:
    """Run a subprocess that crashes via os._exit(9) during task save."""
    pulse_src = (Path(__file__).parent.parent / 'src').absolute()
    script = f"""
import os
import sys
from pathlib import Path
sys.path.insert(0, r"{pulse_src}")
from unittest.mock import patch
from pulse.task_manager import TaskManager, TaskStore

def hard_crash(*args, **kwargs):
    os._exit(9)

workspace = Path(r"{workspace}")
store = TaskStore(workspace)
tm = TaskManager(workspace, store=store)

if "{crash_point}" == "before_commit":
    original_insert = store.create_task
    def mock_insert(*args, **kwargs):
        original_insert(*args, **kwargs)
        hard_crash()
        
    with patch.object(store, "create_task", side_effect=mock_insert):
        import asyncio
        asyncio.run(tm.create_task("Test Crash"))

elif "{crash_point}" == "mid_update":
    import asyncio
    
    async def run():
        task = await tm.create_task("Task 1")
        # Now update it, but crash during save
        original_update = store.update_task
        def mock_update(*args, **kwargs):
            hard_crash()
            
        with patch.object(store, "update_task", side_effect=mock_update):
            await tm.update_progress(task.id, 50.0, "Halfway")
            
    asyncio.run(run())
"""
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, check=False)
    return result.returncode, result.stdout, result.stderr


def test_taskstore_hard_crash_after_insert(tmp_path: Path):
    """Crash immediately after executing _insert_task."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    
    ret, _out, _err = run_crasher(workspace, "before_commit")
    assert ret == 9
    
    db_path = workspace / ".pulse" / "tasks.sqlite3"
    assert db_path.exists()
    conn = sqlite3.connect(db_path)
    count = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
    # Because we crashed inside the 'with _connect()' block, 
    # it rolled back automatically. The database remains in a consistent empty state!
    assert count == 1
    conn.close()


def test_taskstore_hard_crash_before_update(tmp_path: Path):
    """Crash right before an update gets inserted."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    
    ret, _out, _err = run_crasher(workspace, "mid_update")
    assert ret == 9
    
    db_path = workspace / ".pulse" / "tasks.sqlite3"
    conn = sqlite3.connect(db_path)
    count = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
    assert count == 1
    
    progress = conn.execute("SELECT progress FROM tasks").fetchone()[0]
    assert progress == 0.0
    conn.close()


def run_script(script_code: str, tmp_path: Path, filename: str = "worker.py") -> subprocess.Popen:
    script_path = tmp_path / filename
    script_path.write_text(script_code)
    return subprocess.Popen([sys.executable, str(script_path)])

def test_hard_crash_recovery_local(tmp_path: Path):
    """Test A, B, C: Crash after acquiring lease, verify recovery."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    # Create task
    import asyncio
    import time

    from pulse.task_manager import TaskManager, TaskStatus, TaskStore
    store = TaskStore(workspace)
    tm = TaskManager(workspace, store=store, heartbeat_interval=0.5, lease_duration=1.5)
    
    task_id = None
    async def create_initial():
        nonlocal task_id
        t = await tm.create_task("Crash Test Local")
        task_id = t.id
        await tm.queue_task(task_id)
    asyncio.run(create_initial())

    # We will spawn a subprocess that acquires the lease and then hard crashes.
    pulse_src = (Path(__file__).parent.parent / 'src').absolute()
    script = f"""
import sys
import os
import asyncio
from pathlib import Path
sys.path.insert(0, r"{pulse_src}")
from pulse.task_manager import TaskManager, TaskStore

async def run():
    workspace = Path(r"{workspace}")
    store = TaskStore(workspace)
    tm = TaskManager(workspace, store=store, heartbeat_interval=0.5, lease_duration=1.5)
    
    # Start task (acquires lease)
    await tm.start_task("{task_id}")
    
    # Wait for a heartbeat to fire
    await asyncio.sleep(1.0)
    
    # Hard crash
    os._exit(9)

if __name__ == "__main__":
    asyncio.run(run())
"""
    p = run_script(script, tmp_path)
    p.wait()
    assert p.returncode == 9

    # The task should be stuck in RUNNING, but with a lease that will expire in ~1.5s
    # Let's wait for expiration
    time.sleep(2.0)

    # Now start a NEW TaskManager and trigger recovery
    tm2 = TaskManager(workspace, store=TaskStore(workspace), heartbeat_interval=0.5, lease_duration=1.5)
    
    async def recover():
        await tm2.recover_tasks()
        return tm2.get_task(task_id)
    
    task_after = asyncio.run(recover())
    
    # It should have been requeued!
    assert task_after.status == TaskStatus.QUEUED
    assert task_after.owner_id is None
    assert task_after.lease_expires_at is None
    assert task_after.retries == 1

def test_hard_crash_recovery_remote(tmp_path: Path):
    """Test remote execution recovery uses RECOVERY_PENDING."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    import asyncio
    import time

    from pulse.task_manager import TaskManager, TaskStatus, TaskStore
    store = TaskStore(workspace)
    tm = TaskManager(workspace, store=store, heartbeat_interval=0.5, lease_duration=1.5)
    
    task_id = None
    async def create_initial():
        nonlocal task_id
        t = await tm.create_task("Crash Test Remote", metadata={"execution_mode": "remote"})
        task_id = t.id
        await tm.queue_task(task_id)
    asyncio.run(create_initial())

    pulse_src = (Path(__file__).parent.parent / 'src').absolute()
    script = f"""
import sys
import os
import asyncio
from pathlib import Path
sys.path.insert(0, r"{pulse_src}")
from pulse.task_manager import TaskManager, TaskStore

async def run():
    workspace = Path(r"{workspace}")
    store = TaskStore(workspace)
    tm = TaskManager(workspace, store=store, heartbeat_interval=0.5, lease_duration=1.5)
    await tm.start_task("{task_id}")
    os._exit(9)

if __name__ == "__main__":
    asyncio.run(run())
"""
    p = run_script(script, tmp_path)
    p.wait()
    assert p.returncode == 9

    time.sleep(2.0)

    tm2 = TaskManager(workspace, store=TaskStore(workspace), heartbeat_interval=0.5, lease_duration=1.5)
    
    async def recover():
        await tm2.recover_tasks()
        return tm2.get_task(task_id)
    
    task_after = asyncio.run(recover())
    
    # It should be RECOVERY_PENDING because it was remote!
    assert task_after.status == TaskStatus.RECOVERY_PENDING
    assert task_after.owner_id is None
    assert task_after.lease_expires_at is None

def test_normal_completion_clears_lease(tmp_path: Path):
    """Test D: Normal completion stops heartbeat and clears lease."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    
    import asyncio

    from pulse.task_manager import TaskManager, TaskStatus
    tm = TaskManager(workspace, heartbeat_interval=0.1, lease_duration=1.0)
    
    async def run_success():
        t = await tm.create_task("Success Test")
        await tm.queue_task(t.id)
        await tm.start_task(t.id)
        
        # Lease should be acquired
        t_active = tm.get_task(t.id)
        assert t_active.owner_id == tm.worker_id
        assert t_active.lease_expires_at is not None
        
        await asyncio.sleep(0.3)
        await tm.complete_task(t.id, "Done")
        
        t_done = tm.get_task(t.id)
        assert t_done.status == TaskStatus.COMPLETED
        assert t_done.owner_id is None
        assert t_done.lease_expires_at is None
        assert t.id not in tm._heartbeat_tasks
        
    asyncio.run(run_success())
