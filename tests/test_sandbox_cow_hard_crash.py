import subprocess
import sys
from pathlib import Path

import pytest

from pulse.sandbox.filesystem import CoWFilesystem


def setup_workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "file1.txt").write_text("file1 original", encoding="utf-8")
    (workspace / "file2.txt").write_text("file2 original", encoding="utf-8")
    (workspace / "to_delete.txt").write_text("delete me", encoding="utf-8")
    return workspace

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

def run_crasher(workspace: Path, crash_point: str) -> int:
    """Run a subprocess that will deliberately crash itself via os._exit(9)."""
    pulse_src = (Path(__file__).parent.parent / 'src').absolute()
    script = f"""
import os
import sys
from pathlib import Path
sys.path.insert(0, r"{pulse_src}")
from unittest.mock import patch
from pulse.sandbox.filesystem import CoWFilesystem

def hard_crash(msg):
    sys.stdout.write(f"CRASHING: {{msg}}\\n")
    sys.stdout.flush()
    os._exit(9)

workspace = Path(r"{workspace}")
cow = CoWFilesystem(workspace)
tx = cow.create_transaction()
cow.stage_write(tx, "file1.txt", "file1 modified")
cow.stage_write(tx, "file2.txt", "file2 modified")
cow.stage_delete(tx, "to_delete.txt")
cow.stage_write(tx, "new_file.txt", "new file")
cow.stage_write(tx, "new_dir/new_file.txt", "in new dir")

if "{crash_point}" == "before_wal_durability":
    with patch("os.fsync", side_effect=lambda *args: hard_crash("before_wal_durability")):
        cow.commit_transaction(tx)
        
elif "{crash_point}" == "after_wal_durability":
    # Crash right before writing commit.ready
    original_open = open
    def mock_open(file, *args, **kwargs):
        if str(file).endswith("commit.ready"):
            hard_crash("after_wal_durability")
        return original_open(file, *args, **kwargs)
    with patch("builtins.open", side_effect=mock_open):
        cow.commit_transaction(tx)
        
elif "{crash_point}" == "after_commit_ready":
    # Crash right at the start of _apply_wal
    with patch.object(cow, "_apply_wal", side_effect=lambda *args: hard_crash("after_commit_ready")):
        cow.commit_transaction(tx)
        
elif "{crash_point}" == "after_one_operation":
    original_replace = os.replace
    count = 0
    def mock_replace(src, dst):
        global count
        count += 1
        original_replace(src, dst)
        if count == 1:
            hard_crash("after_one_operation")
    with patch("os.replace", side_effect=mock_replace):
        cow.commit_transaction(tx)
        
elif "{crash_point}" == "midway_multiple":
    original_replace = os.replace
    count = 0
    def mock_replace(src, dst):
        global count
        count += 1
        original_replace(src, dst)
        if count == 2:
            hard_crash("midway_multiple")
    with patch("os.replace", side_effect=mock_replace):
        cow.commit_transaction(tx)

elif "{crash_point}" == "during_deletion":
    original_unlink = Path.unlink
    count = 0
    def mock_unlink(self, *args, **kwargs):
        global count
        count += 1
        original_unlink(self, *args, **kwargs)
        if count == 1:
            hard_crash("during_deletion")
    with patch.object(Path, "unlink", autospec=True, side_effect=mock_unlink):
        cow.commit_transaction(tx)

elif "{crash_point}" == "during_new_file":
    original_replace = os.replace
    def mock_replace(src, dst):
        original_replace(src, dst)
        if "new_file.txt" in str(dst) and "new_dir" not in str(dst):
            hard_crash("during_new_file")
    with patch("os.replace", side_effect=mock_replace):
        cow.commit_transaction(tx)

elif "{crash_point}" == "during_mkdir":
    original_mkdir = Path.mkdir
    def mock_mkdir(self, *args, **kwargs):
        if "new_dir" in self.parts:
            hard_crash("during_mkdir")
        original_mkdir(self, *args, **kwargs)
    with patch.object(Path, "mkdir", autospec=True, side_effect=mock_mkdir):
        cow.commit_transaction(tx)
"""
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, check=False)
    return result.returncode, result.stdout, result.stderr

@pytest.mark.parametrize("crash_point, expected_complete", [
    ("before_wal_durability", False),
    ("after_wal_durability", False),
    ("after_commit_ready", True),
    ("after_one_operation", True),
    ("midway_multiple", True),
    ("during_deletion", True),
    ("during_new_file", True),
    ("during_mkdir", True),
])
def test_hard_crash_recovery(tmp_path: Path, crash_point: str, expected_complete: bool):
    """P1: Genuine hard-crash process tests."""
    workspace = setup_workspace(tmp_path)
    
    # Run the crashing process
    ret, stdout, stderr = run_crasher(workspace, crash_point)
    if ret != 9:
        print(f"STDOUT:\n{stdout}")
        print(f"STDERR:\n{stderr}")
    assert ret == 9  # Verify it genuinely hard crashed with os._exit(9)
    
    # Restart simulation: The parent process spins up a new CoWFilesystem
    CoWFilesystem(workspace)
    
    if expected_complete:
        verify_complete_new_state(workspace)
    else:
        verify_original_state(workspace)
