import sys
import subprocess
from pathlib import Path
import pytest
from pulse.task_manager import TaskManager, TaskStore, TaskConcurrencyError
import asyncio

def test_taskstore_occ_conflict(tmp_path: Path):
    """Verify OCC rejects conflicting updates from different processes (Scenario A & B)."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    
    # Init DB and create a task
    store = TaskStore(workspace)
    tm = TaskManager(workspace, store=store)
    
    task_id = None
    async def create_initial():
        nonlocal task_id
        t = await tm.create_task("Conflict Test")
        task_id = t.id
    asyncio.run(create_initial())

    # Script that simulates a stale writer
    script = f"""
import sys
import asyncio
from pathlib import Path
sys.path.insert(0, r"{workspace.parent.parent.parent.absolute() / 'src'}")
from pulse.task_manager import TaskManager, TaskStore, TaskConcurrencyError

async def run():
    workspace = Path(r"{workspace}")
    store = TaskStore(workspace)
    tm = TaskManager(workspace, store=store)
    
    # Update progress. This will load version 1, modify, and save version 2.
    if sys.argv[1] == "writer_a":
        await tm.update_progress("{task_id}", 50.0, "progress by A")
        
    elif sys.argv[1] == "writer_b":
        # Wait a moment to ensure A writes first, making our in-memory state stale!
        # But wait, we load our state FIRST!
        # Actually, in a subprocess, it will load it on TaskManager init.
        # But to guarantee A commits first, B can just try to write blindly after A finishes.
        # We'll just have python test script orchestrate them sequentially 
        # to prove B rejects if it tries to write stale data.
        pass

if __name__ == "__main__":
    asyncio.run(run())
"""
    script_path = tmp_path / "worker.py"
    script_path.write_text(script)
    
    # Process A writes successfully
    subprocess.run([sys.executable, str(script_path), "writer_a"], check=True)
    
    # Now in the MAIN process, we hold a STALE reference (version 1)
    # Our `tm` instance loaded the tasks at the beginning!
    assert tm.get_task(task_id).version == 1
    
    # If the MAIN process tries to update it now, it should get TaskConcurrencyError
    async def try_stale_write():
        with pytest.raises(TaskConcurrencyError):
            await tm.complete_task(task_id, "done by main")
            
    asyncio.run(try_stale_write())
    
    # Furthermore, verify that because TaskConcurrencyError was caught, TaskManager reloaded the state!
    # Meaning the in-memory cache healed!
    assert tm.get_task(task_id).version == 2
    assert tm.get_task(task_id).progress == 50.0

def test_taskstore_occ_concurrent_progress(tmp_path: Path):
    """Scenario C: Concurrent progress updates on the same task."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    
    store = TaskStore(workspace)
    tm = TaskManager(workspace, store=store)
    
    task_id = None
    async def create_initial():
        nonlocal task_id
        t = await tm.create_task("Race Test")
        task_id = t.id
    asyncio.run(create_initial())

    # Worker script that retries on TaskConcurrencyError
    script = f"""
import sys
import asyncio
from pathlib import Path
sys.path.insert(0, r"{workspace.parent.parent.parent.absolute() / 'src'}")
from pulse.task_manager import TaskManager, TaskStore, TaskConcurrencyError

async def run():
    workspace = Path(r"{workspace}")
    store = TaskStore(workspace)
    tm = TaskManager(workspace, store=store)
    
    # Try to add progress 5 times.
    for i in range(5):
        while True:
            try:
                # We fetch the current progress and add 1
                task = tm.get_task("{task_id}")
                new_prog = task.progress + 1
                await tm.update_progress("{task_id}", new_prog)
                break # success
            except TaskConcurrencyError:
                # The cache was healed by TaskManager! We can just retry!
                pass

if __name__ == "__main__":
    asyncio.run(run())
"""
    script_path = tmp_path / "worker.py"
    script_path.write_text(script)
    
    procs = []
    for _ in range(5):
        p = subprocess.Popen([sys.executable, str(script_path)])
        procs.append(p)
        
    for p in procs:
        p.wait()
        assert p.returncode == 0
        
    # Reload in main to check final state
    store = TaskStore(workspace)
    tm = TaskManager(workspace, store=store)
    final_task = tm.get_task(task_id)
    
    # 5 workers * 5 updates each = 25 total progress added
    assert final_task.progress == 25.0
    # version should be 1 + 25 = 26
    assert final_task.version == 26
