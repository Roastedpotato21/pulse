from __future__ import annotations  # noqa: I001
from dataclasses import asdict

import json
import textwrap
from pathlib import Path

from pulse.eval.trajectory_logger import TrajectoryLogger, TrajectoryStep
from pulse.eval.verifier import PatchVerifier


def _create_buggy_workspace(tmp_path: Path) -> tuple[Path, str]:
    """Create a minimal project with a failing test.
    Returns (workspace_path, patch_text) where *patch_text* fixes the bug.
    """
    # module with bug
    module_path = tmp_path / "module.py"
    module_path.write_text(
        "def foo():\n    return 1\n", encoding="utf-8"
    )
    # test expecting correct behavior
    test_path = tmp_path / "test_module.py"
    test_path.write_text(
        "from module import foo\n\ndef test_foo():\n    assert foo() == 2\n", encoding="utf-8"
    )
    # unified diff that fixes the bug
    patch = textwrap.dedent(
        """\
        --- a/module.py
        +++ b/module.py
        @@ -1,2 +1,2 @@
         def foo():
        -    return 1
        +    return 2
        """
    )
    return tmp_path, patch.strip()


def test_patch_verifier_fail_to_pass(tmp_path: Path):
    workspace, good_patch = _create_buggy_workspace(tmp_path)
    verifier = PatchVerifier(workspace=workspace)
    # Verify that the patch resolves the failing test
    metrics = verifier.verify("foo returns wrong value", good_patch)
    assert metrics["initial_test_result"] == "fail"
    assert metrics["post_apply_test_result"] == "pass", metrics
    assert metrics["fail_to_pass"] is True
    assert metrics["pass_to_pass"] is False


def test_patch_verifier_no_change(tmp_path: Path):
    workspace, _ = _create_buggy_workspace(tmp_path)
    verifier = PatchVerifier(workspace=workspace)
    # Apply an empty patch (no changes) – should remain failing
    metrics = verifier.verify("no patch applied", "")
    assert metrics["initial_test_result"] == "fail"
    assert metrics["post_apply_test_result"] == "fail"
    assert metrics["fail_to_pass"] is False
    assert metrics["pass_to_pass"] is False


def test_patch_verifier_does_not_credit_an_invalid_patch(tmp_path: Path):
    workspace, _ = _create_buggy_workspace(tmp_path)
    metrics = PatchVerifier(workspace=workspace).verify(
        "invalid patch", "--- a/missing.py\n+++ b/missing.py\n@@ -1 +1 @@\n-x\n+y\n"
    )
    assert metrics["post_apply_test_result"] == "fail"
    assert metrics["fail_to_pass"] is False
    assert "error:" in metrics["pytest_stderr"]


def test_trajectory_logger_roundtrip(tmp_path: Path):
    logger = TrajectoryLogger(workspace=tmp_path, task_id="test123")
    logger.add_step(prompt="first", tool_name="status", reasoning="init", token_cost=10)
    logger.add_step(prompt="second", tool_name="edit", reasoning="fix bug", token_cost=25, result="ok")
    out_path = logger.dump()
    # Ensure file exists and content is valid JSON
    assert out_path.is_file()
    raw = json.loads(out_path.read_text(encoding="utf-8"))
    assert raw["schema_version"] == 1
    assert len(raw["steps"]) == 2
    # Load back and compare objects
    loaded_steps = logger.load()
    assert len(loaded_steps) == 2
    first, second = loaded_steps
    assert isinstance(first, TrajectoryStep)
    assert first.prompt == "first"
    assert second.result == "ok"
    # Ensure the loaded steps match the original dump content
    assert [asdict(s) for s in loaded_steps] == raw["steps"]
