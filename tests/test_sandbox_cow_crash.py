"""Adversarial crash recovery tests for CoWFilesystem.

Tests simulate hard process crashes during the commit phase by injecting
exceptions at precise moments. After each crash, we verify the workspace
is NOT in a mixed state. We then simulate a process restart by creating a
new CoWFilesystem, which should automatically roll forward the WAL redo log
if the crash happened during Phase 2.
"""

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from pulse.sandbox.filesystem import CoWFilesystem


class SimulatedCrash(Exception):
    """Exception raised to simulate a hard process crash."""


def setup_workspace(tmp_path: Path) -> tuple[Path, CoWFilesystem]:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    
    # Pre-populate workspace
    (workspace / "file1.txt").write_text("file1 original", encoding="utf-8")
    (workspace / "file2.txt").write_text("file2 original", encoding="utf-8")
    (workspace / "to_delete.txt").write_text("delete me", encoding="utf-8")
    
    cow = CoWFilesystem(workspace)
    return workspace, cow


def verify_original_state(workspace: Path):
    assert (workspace / "file1.txt").read_text(encoding="utf-8") == "file1 original"
    assert (workspace / "file2.txt").read_text(encoding="utf-8") == "file2 original"
    assert (workspace / "to_delete.txt").read_text(encoding="utf-8") == "delete me"
    assert not (workspace / "new_file.txt").exists()
    assert not (workspace / "new_dir" / "new_file.txt").exists()


def verify_complete_new_state(workspace: Path):
    assert (workspace / "file1.txt").read_text(encoding="utf-8") == "file1 modified"
    assert (workspace / "file2.txt").read_text(encoding="utf-8") == "file2 modified"
    assert not (workspace / "to_delete.txt").exists()
    assert (workspace / "new_file.txt").read_text(encoding="utf-8") == "new file"
    assert (workspace / "new_dir" / "new_file.txt").read_text(encoding="utf-8") == "in new dir"


def stage_changes(cow: CoWFilesystem):
    tx = cow.create_transaction()
    cow.stage_write(tx, "file1.txt", "file1 modified")
    cow.stage_write(tx, "file2.txt", "file2 modified")
    cow.stage_delete(tx, "to_delete.txt")
    cow.stage_write(tx, "new_file.txt", "new file")
    cow.stage_write(tx, "new_dir/new_file.txt", "in new dir")
    return tx


def test_crash_before_first_file_operation(tmp_path: Path):
    """Crash point 1: Before the first file operation."""
    workspace, cow = setup_workspace(tmp_path)
    tx = stage_changes(cow)
    
    # We will crash during Phase 1 fsync of commit.wal
    with patch("os.fsync", side_effect=SimulatedCrash("Crash during WAL fsync")), pytest.raises(SimulatedCrash):
            cow.commit_transaction(tx)
            
    # The WAL marker wasn't written. We should be exactly in original state.
    verify_original_state(workspace)
    
    # Restart simulation
    CoWFilesystem(workspace)
    
    # Still original state
    verify_original_state(workspace)


def test_crash_after_one_file_written(tmp_path: Path):
    """Crash point 2: After one file is written."""
    workspace, cow = setup_workspace(tmp_path)
    tx = stage_changes(cow)
    
    original_replace = os.replace
    replace_count = 0
    
    def mocked_replace(src, dst):
        nonlocal replace_count
        replace_count += 1
        original_replace(src, dst)
        if replace_count == 1:
            raise SimulatedCrash("Crash after 1st file written")
            
    with patch("os.replace", side_effect=mocked_replace), pytest.raises(SimulatedCrash):
            cow.commit_transaction(tx)
            
    # Mixed state actually exists here while the process is "dead".
    # We simulate restart.
    CoWFilesystem(workspace)
    
    # Redo log completes the transaction
    verify_complete_new_state(workspace)


def test_crash_halfway_through_multiple_writes(tmp_path: Path):
    """Crash point 3: Halfway through multiple writes."""
    workspace, cow = setup_workspace(tmp_path)
    tx = stage_changes(cow)
    
    original_replace = os.replace
    replace_count = 0
    
    def mocked_replace(src, dst):
        nonlocal replace_count
        replace_count += 1
        original_replace(src, dst)
        if replace_count == 2:
            raise SimulatedCrash("Crash after 2nd file written")
            
    with patch("os.replace", side_effect=mocked_replace), pytest.raises(SimulatedCrash):
            cow.commit_transaction(tx)
            
    CoWFilesystem(workspace)
    verify_complete_new_state(workspace)


def test_crash_after_deletion(tmp_path: Path):
    """Crash point 4: After a deletion."""
    workspace, cow = setup_workspace(tmp_path)
    tx = stage_changes(cow)
    
    original_unlink = Path.unlink
    unlink_count = 0
    
    def mocked_unlink(self, *args, **kwargs):
        nonlocal unlink_count
        unlink_count += 1
        original_unlink(self, *args, **kwargs)
        if unlink_count == 1:
            raise SimulatedCrash("Crash after 1st deletion")
            
    with patch.object(Path, "unlink", autospec=True, side_effect=mocked_unlink), pytest.raises(SimulatedCrash):
            cow.commit_transaction(tx)
            
    CoWFilesystem(workspace)
    verify_complete_new_state(workspace)


def test_crash_after_new_file_created(tmp_path: Path):
    """Crash point 5: After a new file is created."""
    workspace, cow = setup_workspace(tmp_path)
    tx = stage_changes(cow)
    
    original_replace = os.replace
    
    def mocked_replace(src, dst):
        original_replace(src, dst)
        if "new_file.txt" in str(dst) and "new_dir" not in str(dst):
            raise SimulatedCrash("Crash after new file written")
            
    with patch("os.replace", side_effect=mocked_replace), pytest.raises(SimulatedCrash):
            cow.commit_transaction(tx)
            
    CoWFilesystem(workspace)
    verify_complete_new_state(workspace)


def test_crash_during_directory_creation(tmp_path: Path):
    """Crash point 6: During directory creation."""
    workspace, cow = setup_workspace(tmp_path)
    tx = stage_changes(cow)
    
    original_mkdir = Path.mkdir
    
    def mocked_mkdir(self, *args, **kwargs):
        if "new_dir" in self.parts:
            # We crash before it's actually created
            raise SimulatedCrash("Crash during mkdir")
        original_mkdir(self, *args, **kwargs)
            
    with patch.object(Path, "mkdir", autospec=True, side_effect=mocked_mkdir), pytest.raises(SimulatedCrash):
            cow.commit_transaction(tx)
            
    CoWFilesystem(workspace)
    verify_complete_new_state(workspace)


def test_crash_immediately_before_commit_finalization(tmp_path: Path):
    """Crash point 7: Immediately before commit finalization."""
    workspace, cow = setup_workspace(tmp_path)
    tx = stage_changes(cow)
    
    
    def mocked_discard(tx_param):
        raise SimulatedCrash("Crash right before final cleanup")
            
    with patch.object(cow, "discard_transaction", side_effect=mocked_discard), pytest.raises(SimulatedCrash):
            cow.commit_transaction(tx)
            
    # In this case, all files are fully written, but staging dir wasn't discarded
    # We still verify the complete new state before restart to prove redo is idempotent
    verify_complete_new_state(workspace)
    
    CoWFilesystem(workspace)
    verify_complete_new_state(workspace)
    
    # Verify staging dir is gone
    assert not tx.staging_dir.exists()


def test_crash_after_commit_finalization(tmp_path: Path):
    """Crash point 8: After commit finalization."""
    # If a crash happens after commit finalization, it returns normally to the caller.
    # The staging directory is already gone.
    # We simulate crashing the caller by just acting as if we return and then crash.
    workspace, cow = setup_workspace(tmp_path)
    tx = stage_changes(cow)
    
    cow.commit_transaction(tx)
    verify_complete_new_state(workspace)
    assert not tx.staging_dir.exists()
    
    # Restart
    CoWFilesystem(workspace)
    verify_complete_new_state(workspace)
