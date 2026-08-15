import os
import sys
import subprocess
from pathlib import Path
import pytest
import sqlite3

def run_crasher(workspace: Path, crash_point: str) -> tuple[int, str, str]:
    """Run a subprocess that crashes via os._exit(9) during task save."""
    script = f"""
import os
import sys
from pathlib import Path
sys.path.insert(0, r"{workspace.parent.parent.parent.absolute() / 'src'}")
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
    
    ret, out, err = run_crasher(workspace, "before_commit")
    assert ret == 9
    
    import sqlite3
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
    
    ret, out, err = run_crasher(workspace, "mid_update")
    assert ret == 9
    
    import sqlite3
    db_path = workspace / ".pulse" / "tasks.sqlite3"
    conn = sqlite3.connect(db_path)
    count = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
    assert count == 1
    
    progress = conn.execute("SELECT progress FROM tasks").fetchone()[0]
    assert progress == 0.0
    conn.close()
