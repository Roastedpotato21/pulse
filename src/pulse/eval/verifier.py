from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, TypedDict

TestResult = Literal["pass", "fail"]

class PatchVerificationMetrics(TypedDict):
    """Outcome metrics for a candidate fix."""
    issue_description: str
    patch: str
    initial_test_result: TestResult
    post_apply_test_result: TestResult
    fail_to_pass: bool
    pass_to_pass: bool
    pytest_stdout: str
    pytest_stderr: str

@dataclass(slots=True)
class PatchVerifier:
    """Applies a diff to a copy of the repository and evaluates test outcomes."""

    workspace: Path
    """Root of the original Pulse project."""

    def _run_pytest(self, directory: Path) -> tuple[int, str, str]:
        """Execute ``pytest`` in *directory* and return (returncode, stdout, stderr)."""
        try:
            proc = subprocess.run(
                [sys.executable, "-m", "pytest", "-p", "no:cacheprovider"],
                cwd=directory,
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            )
            return proc.returncode, proc.stdout, proc.stderr
        except subprocess.TimeoutExpired as exc:
            return -1, "", f"Timeout: {exc}"

    def _apply_patch(self, target_dir: Path, patch_text: str) -> tuple[bool, str]:
        """Validate and apply a unified diff without allowing paths outside the copy."""
        if not patch_text:
            return False, "Patch is empty."
        try:
            normalized_patch = patch_text if patch_text.endswith("\n") else patch_text + "\n"
            for mode in ("--check", "--apply"):
                proc = subprocess.run(
                    ["git", "apply", mode, "--whitespace=nowarn", "-"],
                    cwd=target_dir,
                    input=normalized_patch,
                    capture_output=True,
                    text=True,
                    timeout=30,
                    check=False,
                )
                if proc.returncode != 0:
                    detail = proc.stderr.strip() or proc.stdout.strip() or "git apply failed"
                    return False, detail
            return True, ""
        except (OSError, subprocess.TimeoutExpired) as exc:
            return False, f"Patch application failed: {exc}"

    def verify(self, issue_description: str, patch: str) -> PatchVerificationMetrics:
        """Run the verification cycle and return detailed metrics.
        Steps:
        1. Run pytest on the original workspace (baseline).
        2. Copy the workspace to a temporary isolated directory.
        3. Apply the supplied *patch* in the temporary copy.
        4. Re‑run pytest on the patched copy.
        5. Compute pass/fail transitions and return the metrics dict.
        """
        # Baseline run
        init_rc, _init_out, _init_err = self._run_pytest(self.workspace)
        initial_result: TestResult = "pass" if init_rc == 0 else "fail"

        # Isolated copy and patch application
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            ignored = {
                ".git",
                ".mypy_cache",
                ".pytest_cache",
                ".ruff_cache",
                ".venv",
                "__pycache__",
                "build",
                "dist",
                "venv",
            }
            for item in self.workspace.iterdir():
                if item.name in ignored:
                    continue
                dest = tmp_path / item.name
                if item.is_dir():
                    shutil.copytree(
                        item,
                        dest,
                        dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns(*ignored),
                    )
                else:
                    shutil.copy2(item, dest)

            applied, apply_error = self._apply_patch(tmp_path, patch)
            if applied:
                post_rc, post_out, post_err = self._run_pytest(tmp_path)
            else:
                post_rc, post_out, post_err = -1, "", apply_error

        post_result: TestResult = "pass" if post_rc == 0 else "fail"

        return {
            "issue_description": issue_description,
            "patch": patch,
            "initial_test_result": initial_result,
            "post_apply_test_result": post_result,
            "fail_to_pass": initial_result == "fail" and post_result == "pass",
            "pass_to_pass": initial_result == "pass" and post_result == "pass",
            "pytest_stdout": post_out,
            "pytest_stderr": post_err,
        }
