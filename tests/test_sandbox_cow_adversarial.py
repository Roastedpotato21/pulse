import json
import logging
from pathlib import Path

import pytest

from pulse.sandbox.filesystem import CoWFilesystem


def setup_workspace(tmp_path: Path) -> tuple[Path, CoWFilesystem]:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "file.txt").write_text("content", encoding="utf-8")
    
    cow = CoWFilesystem(workspace)
    return workspace, cow


def create_malicious_wal(cow: CoWFilesystem, payload: str, tmp_path: Path):
    tx_id = "malicious_tx"
    staging_dir = cow._staging_base / tx_id
    staging_dir.mkdir(parents=True, exist_ok=True)
    
    # We only write dummy content if the payload isn't malicious by design
    # If the payload is malicious, we just want to ensure it gets blocked during path validation
    # in _apply_wal. We don't want the test itself to attempt path traversal writing!
    if "outside" not in payload and "cmd.exe" not in payload and "server" not in payload and "shadow" not in payload:
        staged_path = staging_dir / payload
        staged_path.parent.mkdir(parents=True, exist_ok=True)
        staged_path.write_text("malicious_content")
            
    
    wal_data = {
        "transaction_id": tx_id,
        "operations": [
            {
                "action": "write",
                "path": payload,
            }
        ]
    }
    
    (staging_dir / "commit.wal").write_text(json.dumps(wal_data))
    (staging_dir / "commit.ready").touch()
    return staging_dir


@pytest.mark.parametrize("payload", [
    "../../outside.txt",  # Traversal up
    "/etc/shadow",        # Absolute POSIX
    "C:\\Windows\\System32\\cmd.exe", # Absolute Windows
    "\\\\server\\share\\file", # Windows UNC
])
def test_malicious_wal_path_traversal(tmp_path: Path, payload: str, caplog):
    """P0: Verify that path validation during recovery prevents traversal."""
    workspace, cow = setup_workspace(tmp_path)
    
    # Create an external file to ensure it doesn't get touched
    external_file = tmp_path / "outside.txt"
    external_file.write_text("original_external_content")
    
    # Stage the malicious transaction manually (as if an attacker modified commit.wal)
    staging_dir = create_malicious_wal(cow, payload, tmp_path)
    
    with caplog.at_level(logging.ERROR):
        # Trigger initialization, which invokes _cleanup_orphaned_staging
        CoWFilesystem(workspace)
        
    # Verify fail-closed behavior:
    # 1. The error must be logged
    assert "WAL Recovery Failed" in caplog.text
    assert "Malicious or invalid path in WAL" in caplog.text
    
    # 2. The staging directory must NOT be deleted, allowing forensic investigation
    assert staging_dir.exists()
    
    # 3. External files must NOT be touched
    assert external_file.read_text() == "original_external_content"
    if payload == "/etc/shadow":
        assert not Path("/etc/shadow").exists() or Path("/etc/shadow").stat().st_size > 0 # Hard to test root system files directly, but ensuring no empty file was written


def test_malicious_wal_delete_traversal(tmp_path: Path, caplog):
    """P0: Verify that deletes also prevent traversal."""
    workspace, cow = setup_workspace(tmp_path)
    
    external_file = tmp_path / "outside_delete.txt"
    external_file.write_text("dont_delete_me")
    
    tx_id = "malicious_delete"
    staging_dir = cow._staging_base / tx_id
    staging_dir.mkdir(parents=True, exist_ok=True)
    
    wal_data = {
        "transaction_id": tx_id,
        "operations": [
            {
                "action": "delete",
                "path": "../../outside_delete.txt",
            }
        ]
    }
    
    (staging_dir / "commit.wal").write_text(json.dumps(wal_data))
    (staging_dir / "commit.ready").touch()
    
    with caplog.at_level(logging.ERROR):
        CoWFilesystem(workspace)
        
    assert "WAL Recovery Failed" in caplog.text
    assert external_file.exists()
    assert external_file.read_text() == "dont_delete_me"
