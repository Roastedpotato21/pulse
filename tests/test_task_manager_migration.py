import json
from pathlib import Path

from pulse.task_manager import TaskManager, TaskStore


def test_legacy_json_migration(tmp_path: Path):
    """Verify legacy tasks.json is safely migrated to SQLite."""
    workspace = tmp_path / "workspace"
    store_dir = workspace / ".pulse"
    store_dir.mkdir(parents=True)
    
    legacy_file = store_dir / "tasks.json"
    
    # Create fake legacy JSON
    legacy_data = {
        "task-123": {
            "id": "task-123",
            "title": "Legacy Task",
            "goal": "Migrate me",
            "priority": "HIGH",
            "status": "COMPLETED",
            "progress": 100.0,
            "retries": 0,
            "max_retries": 3,
            "depends_on": [],
            "checkpoints": [],
            "history": [],
            "created_at": "2023-01-01T00:00:00+00:00",
            "updated_at": "2023-01-01T00:00:00+00:00",
            "result": "Success",
            "error": None,
            "metadata": {"legacy": True}
        }
    }
    legacy_file.write_text(json.dumps(legacy_data))
    
    # Init store, triggering migration
    store = TaskStore(workspace)
    tm = TaskManager(workspace, store=store)
    
    # Verify legacy file was backed up
    assert not legacy_file.exists()
    assert (store_dir / "tasks.json.bak").exists()
    
    # Verify data in SQLite
    tasks = tm.list_tasks()
    assert len(tasks) == 1
    task = tasks[0]
    assert task.id == "task-123"
    assert task.title == "Legacy Task"
    assert task.status.value == "COMPLETED"
    assert task.metadata == {"legacy": True}
