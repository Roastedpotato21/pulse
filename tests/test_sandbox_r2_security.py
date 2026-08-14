"""Regression tests for R2 sandbox security vulnerabilities.

Tests:
    - Process tree termination (Finding #1)
    - CoW Optimistic Concurrency Control (Finding #2)
    - Docker overlay secure extraction (Finding #3)
    - Docker ulimit translation (Finding #4)
    - Broken auto-commit fix (Finding #5)
"""

import os
import signal
import sys
import time
from pathlib import Path

import pytest

from pulse.sandbox.api import Sandbox
from pulse.sandbox.backend.docker import DockerBackend
from pulse.sandbox.backend.host import HostBackend
from pulse.sandbox.errors import SandboxConcurrentModificationError
from pulse.sandbox.filesystem import CoWFilesystem
from pulse.sandbox.policy import PolicyDecision, SandboxPolicy
from pulse.sandbox.resources import ResourcePolicy

# ---------------------------------------------------------------------------
# Finding #1: Process Tree Termination
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_process_tree_grandchild_terminated(tmp_path: Path):
    """Verify that killpg() correctly kills a child and its grandchild."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    
    # We must use HostBackend to reliably introspect PIDs.
    _policy = SandboxPolicy(default_decisions={"shell": PolicyDecision.ALLOW, "network": PolicyDecision.ALLOW, "secrets": PolicyDecision.ALLOW})
    sandbox = Sandbox(workspace, backend=HostBackend(), unsafe_host_execution=True, policy=_policy)
    
    # Python script that spawns a child, then both wait infinitely.
    script = """
import os
import subprocess
import sys
import time

child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(999)"])
print(f"CHILD_PID={os.getpid()}")
print(f"GRANDCHILD_PID={child.pid}")
sys.stdout.flush()

try:
    time.sleep(999)
except KeyboardInterrupt:
    pass
"""
    script_path = workspace / "test.py"
    script_path.write_text(script, encoding="utf-8")
    
    policy = SandboxPolicy(default_decisions={"python": PolicyDecision.ALLOW, "shell": PolicyDecision.ALLOW, "network": PolicyDecision.ALLOW, "secrets": PolicyDecision.ALLOW})
    sandbox.policy = policy
    
    # We run it manually to capture the output mid-flight, or run with a short timeout.
    # To test termination, we set a 1-second timeout.
    sandbox.backend.process_manager.limits = ResourcePolicy(wall_time_seconds=1.0)
    result = await sandbox.execute_command(
        [sys.executable, "test.py"],
    )
    
    assert result.timed_out
    
    # Parse PIDs from the truncated output (or full output if it flushed in time).
    out = result.stdout
    grandchild_pid = None
    for line in out.splitlines():
        if line.startswith("CHILD_PID="):
            int(line.split("=")[1])
        elif line.startswith("GRANDCHILD_PID="):
            grandchild_pid = int(line.split("=")[1])
            
    if sys.platform != "win32" and grandchild_pid:
        # Verify the grandchild is dead. os.kill(pid, 0) raises OSError if dead.
        with pytest.raises(OSError):
            os.kill(grandchild_pid, 0)


@pytest.mark.anyio
async def test_sigterm_ignored_sigkill_escalation(tmp_path: Path):
    """Verify that a process ignoring SIGTERM is killed via SIGKILL."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _policy = SandboxPolicy(default_decisions={"shell": PolicyDecision.ALLOW, "network": PolicyDecision.ALLOW, "secrets": PolicyDecision.ALLOW})
    sandbox = Sandbox(workspace, backend=HostBackend(), unsafe_host_execution=True, policy=_policy)
    
    script = """
import signal
import time

def handler(signum, frame):
    print("Caught SIGTERM, ignoring")

signal.signal(signal.SIGTERM, handler)
print("READY")
try:
    while True:
        time.sleep(0.1)
except KeyboardInterrupt:
    pass
"""
    script_path = workspace / "test.py"
    script_path.write_text(script, encoding="utf-8")
    sandbox.policy = SandboxPolicy(default_decisions={"python": PolicyDecision.ALLOW, "shell": PolicyDecision.ALLOW, "network": PolicyDecision.ALLOW, "secrets": PolicyDecision.ALLOW})
    
    # Give it 1 second timeout and 0.5s grace period.
    sandbox.backend.process_manager.limits = ResourcePolicy(wall_time_seconds=0.5, termination_grace_seconds=0.5)
    result = await sandbox.execute_command(
        [sys.executable, "test.py"],
    )
    
    assert result.timed_out
    # Windows taskkill /F doesn't care about SIGTERM traps, but POSIX does.
    if sys.platform != "win32":
        assert "Caught SIGTERM" in result.stdout
    assert result.exit_code != 0


@pytest.mark.anyio
async def test_setsid_escape_documented(tmp_path: Path):
    """Document/verify the POSIX setsid() limitation.
    
    This test proves that a process calling setsid() escapes the HostBackend
    process group kill, reinforcing why Docker is required for isolation.
    """
    if sys.platform == "win32":
        pytest.skip("setsid not applicable on Windows")
        
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    # We must use HostBackend to test the fallback behavior accurately.
    _policy = SandboxPolicy(default_decisions={"shell": PolicyDecision.ALLOW, "network": PolicyDecision.ALLOW, "secrets": PolicyDecision.ALLOW})
    sandbox = Sandbox(workspace, backend=HostBackend(), unsafe_host_execution=True, policy=_policy)
    
    script = """
import os
import subprocess
import sys
import time

# Fork and setsid
pid = os.fork()
if pid == 0:
    os.setsid()
    print(f"ESCAPED_PID={os.getpid()}")
    sys.stdout.flush()
    time.sleep(999)
else:
    time.sleep(999)
"""
    script_path = workspace / "test.py"
    script_path.write_text(script, encoding="utf-8")
    sandbox.policy = SandboxPolicy(default_decisions={"python": PolicyDecision.ALLOW})
    
    result = await sandbox.execute_command(
        [sys.executable, "test.py"],
        limits=ResourcePolicy(wall_time_seconds=0.5)
    )
    
    escaped_pid = None
    for line in result.stdout.splitlines():
        if line.startswith("ESCAPED_PID="):
            escaped_pid = int(line.split("=")[1])
            
    if escaped_pid:
        try:
            # The process should STILL BE ALIVE because it escaped!
            os.kill(escaped_pid, 0)
            # Cleanup so we don't leak it permanently.
            os.kill(escaped_pid, signal.SIGKILL)
        except OSError:
            pytest.fail("Escaped process was unexpectedly killed.")


# ---------------------------------------------------------------------------
# Finding #2: CoW Optimistic Concurrency Control
# ---------------------------------------------------------------------------

def test_cow_external_modification_detected(tmp_path: Path):
    """Verify that modifying a file's content fails the commit."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    
    target = workspace / "config.json"
    target.write_text('{"key": "value"}', encoding="utf-8")
    
    cow = CoWFilesystem(workspace)
    tx = cow.create_transaction()
    
    # Stage the file
    cow.stage_write(tx, "config.json", '{"key": "new_value"}')
    
    # EXTERNALLY modify the file (simulating concurrent process)
    target.write_text('{"key": "externally_modified"}', encoding="utf-8")
    
    # Commit must fail with ConcurrentModificationError
    with pytest.raises(SandboxConcurrentModificationError) as exc:
        cow.commit_transaction(tx)
        
    assert "config.json" in str(exc.value.path)


def test_cow_external_deletion_detected(tmp_path: Path):
    """Verify that deleting a staged file externally fails the commit."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    
    target = workspace / "file.txt"
    target.write_text("hello", encoding="utf-8")
    
    cow = CoWFilesystem(workspace)
    tx = cow.create_transaction()
    cow.stage_write(tx, "file.txt", "goodbye")
    
    # Delete externally
    target.unlink()
    
    with pytest.raises(SandboxConcurrentModificationError) as exc:
        cow.commit_transaction(tx)
    assert exc.value.reason == "file_deleted"


def test_cow_external_replacement_detected(tmp_path: Path):
    """Verify that replacing a file (new inode) fails the commit."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    
    target = workspace / "file.txt"
    target.write_text("hello", encoding="utf-8")
    
    cow = CoWFilesystem(workspace)
    tx = cow.create_transaction()
    cow.stage_write(tx, "file.txt", "goodbye")
    
    # Replace externally (rename over it)
    other = workspace / "other.txt"
    other.write_text("hello", encoding="utf-8")  # Same content and size!
    # Force mtime to be exactly the same if possible (hard to guarantee, but replacement is the key)
    os.replace(other, target)
    
    with pytest.raises(SandboxConcurrentModificationError) as exc:
        cow.commit_transaction(tx)
    assert exc.value.reason in ("file_replaced", "metadata_changed")


def test_cow_modify_restore_detected(tmp_path: Path):
    """Verify that modify->restore (mtime changes) fails the commit."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    
    target = workspace / "file.txt"
    target.write_text("hello", encoding="utf-8")
    original_stat = target.stat()
    
    cow = CoWFilesystem(workspace)
    tx = cow.create_transaction()
    cow.stage_write(tx, "file.txt", "goodbye")
    
    # Touch the file to update its mtime, simulating a restore.
    # On Windows, nanosecond precision might not update if too fast, sleep briefly.
    time.sleep(0.01)
    target.touch()
    
    if target.stat().st_mtime_ns == original_stat.st_mtime_ns:
        pytest.skip("mtime resolution too low to detect fast touch")
        
    with pytest.raises(SandboxConcurrentModificationError) as exc:
        cow.commit_transaction(tx)
    assert exc.value.reason == "metadata_changed"


def test_cow_new_file_commits_successfully(tmp_path: Path):
    """Verify that creating a completely new file works (no snapshot conflict)."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    
    cow = CoWFilesystem(workspace)
    tx = cow.create_transaction()
    cow.stage_write(tx, "new.txt", "content")
    cow.commit_transaction(tx)
    
    assert (workspace / "new.txt").read_text() == "content"


# ---------------------------------------------------------------------------
# Finding #3 & #4: Docker limits and extraction
# ---------------------------------------------------------------------------

def test_docker_ulimit_nofile_and_cpu_generated():
    """Verify DockerBackend translates policy to --ulimit nofile and --ulimit cpu."""
    backend = DockerBackend()
    policy = ResourcePolicy(max_open_files=512, cpu_time_seconds=60)
    
    cmd = backend.build_docker_cmd(
        command="echo hi",
        workspace_root=Path("/workspace"),
        limits=policy
    )
    
    cmd_str = " ".join(cmd)
    assert "--ulimit nofile=512:512" in cmd_str
    assert "--ulimit cpu=60:60" in cmd_str


def test_docker_tar_safety_flags_present():
    """Verify that overlay extraction uses safe tar flags on POSIX."""
    if sys.platform == "win32":
        pytest.skip("Docker tar extraction not used on Windows")
        
    backend = DockerBackend()
    
    # We have to mock the subprocess to inspect the command, since it runs internally.
    # The command is built directly in _extract_overlay.
    import inspect
    source = inspect.getsource(backend._extract_overlay)
    
    assert "--no-same-owner" in source
    assert "--no-same-permissions" in source


@pytest.mark.anyio
async def test_docker_autocommit_uses_staged_changes(tmp_path: Path):
    """Verify that api.py actually commits the overlay (Finding #5 fix)."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    
    sandbox = Sandbox(workspace, unsafe_host_execution=False)
    try:
        await sandbox.initialize()
    except Exception:  # noqa: BLE001
        pytest.skip("Docker not available")
        
    if not sandbox.backend.name in ("docker", "podman"):
        pytest.skip("Backend is not docker")
        
    sandbox.policy = SandboxPolicy(default_decisions={"shell": PolicyDecision.ALLOW, "network": PolicyDecision.ALLOW, "write": PolicyDecision.ALLOW})
    
    # Run a command that writes to the overlay
    result = await sandbox.execute_command(
        ["sh", "-c", "mkdir -p /workspace-overlay/nested && echo 'success' > /workspace-overlay/nested/file.txt"]
    )
    
    assert result.exit_code == 0
    # The file should now exist in the real workspace because auto-commit succeeded
    assert (workspace / "nested" / "file.txt").exists()
    assert (workspace / "nested" / "file.txt").read_text().strip() == "success"


# ---------------------------------------------------------------------------
# P1: PDEATHSIG and Process Containment
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_process_normal_execution(tmp_path: Path):
    """Verify normal execution completes correctly."""
    from pulse.sandbox.process import ProcessManager
    pm = ProcessManager()
    import sys
    result = await pm.execute([sys.executable, "-c", "print('hello')"], cwd=tmp_path)
    assert result.exit_code == 0
    assert "hello" in result.stdout


@pytest.mark.anyio
async def test_process_cancellation(tmp_path: Path):
    """Verify asyncio cancellation terminates the process."""
    from pulse.sandbox.process import ProcessManager
    import asyncio
    pm = ProcessManager()
    
    async def run():
        return await pm.execute([sys.executable, "-c", "import time; time.sleep(999)"], cwd=tmp_path)
        
    task = asyncio.create_task(run())
    await asyncio.sleep(0.5)
    task.cancel()
    
    with pytest.raises(asyncio.CancelledError):
        await task
        

@pytest.mark.anyio
async def test_process_timeout(tmp_path: Path):
    """Verify timeout terminates the process."""
    from pulse.sandbox.process import ProcessManager
    from pulse.sandbox.resources import ResourcePolicy
    pm = ProcessManager()
    result = await pm.execute([sys.executable, "-c", "import time; time.sleep(999)"], cwd=tmp_path, limits=ResourcePolicy(wall_time_seconds=0.5))
    assert result.timed_out


@pytest.mark.anyio
async def test_linux_pdeathsig_receives_signal(tmp_path: Path):
    """Verify PDEATHSIG kills child when parent crashes."""
    if not sys.platform.startswith("linux"):
        pytest.skip("PDEATHSIG is Linux-specific")
        
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    
    script = """
import os
import signal
import sys
import time

with open("child_pid.txt", "w") as f:
    f.write(str(os.getpid()))

time.sleep(999)
"""
    (workspace / "child.py").write_text(script)
    
    parent_script = """
import asyncio
from pathlib import Path
import sys
import os
from pulse.sandbox.api import Sandbox
from pulse.sandbox.backend.host import HostBackend
from pulse.sandbox.policy import SandboxPolicy, PolicyDecision

async def main():
    from pulse.sandbox.process import ProcessManager
    pm = ProcessManager()
    task = asyncio.create_task(pm.execute([sys.executable, "child.py"], cwd=Path(".")))
    await asyncio.sleep(1)
    os._exit(1)

asyncio.run(main())
"""
    (workspace / "parent.py").write_text(parent_script)
    
    import subprocess
    parent_proc = subprocess.Popen([sys.executable, "parent.py"], cwd=workspace)  # noqa: ASYNC220
    parent_proc.wait()
    
    child_pid_path = workspace / "child_pid.txt"
    if not child_pid_path.exists():
        pytest.fail("Child did not write PID file")
        
    child_pid = int(child_pid_path.read_text())
    
    with pytest.raises(OSError):
        os.kill(child_pid, 0)

