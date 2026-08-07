"""Unit tests for Phase 4 of Pulse Sandbox: Copy-on-Write Filesystem and Transactional Mutations."""

from pathlib import Path

from pulse.sandbox.filesystem import CoWFilesystem


def test_cow_staging_isolation(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    real_file = workspace / "hello.txt"
    real_file.write_text("Original content", encoding="utf-8")

    cow = CoWFilesystem(workspace)
    tx = cow.create_transaction()

    # Stage write
    cow.stage_write(tx, "hello.txt", "Modified content")

    # Real workspace must remain untouched prior to commit
    assert real_file.read_text(encoding="utf-8") == "Original content"

    # Preview diff
    diff = cow.preview_changes(tx)
    assert "-Original content" in diff
    assert "+Modified content" in diff

    # Discard transaction
    cow.discard_transaction(tx)
    assert real_file.read_text(encoding="utf-8") == "Original content"
    assert tx.is_discarded is True


def test_cow_atomic_commit(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    real_file = workspace / "src" / "app.py"
    real_file.parent.mkdir()
    real_file.write_text("def old(): pass", encoding="utf-8")

    cow = CoWFilesystem(workspace)
    tx = cow.create_transaction()

    cow.stage_write(tx, "src/app.py", "def new(): pass")
    cow.stage_write(tx, "src/config.json", '{"v": 1}')

    modified = cow.commit_transaction(tx)
    assert "src/app.py" in modified
    assert "src/config.json" in modified
    assert tx.is_committed is True

    # Real workspace must now be updated
    assert real_file.read_text(encoding="utf-8") == "def new(): pass"
    assert (workspace / "src" / "config.json").exists()


def test_cow_stage_delete(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    temp_file = workspace / "temp.log"
    temp_file.write_text("log data", encoding="utf-8")

    cow = CoWFilesystem(workspace)
    tx = cow.create_transaction()

    cow.stage_delete(tx, "temp.log")
    diff = cow.preview_changes(tx)
    assert "-log data" in diff
    assert temp_file.exists()  # Still exists before commit

    cow.commit_transaction(tx)
    assert not temp_file.exists()  # Deleted after commit
