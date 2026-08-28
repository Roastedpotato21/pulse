"""Run the live Docker release suite and fail if any selected test is skipped."""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

LIVE_DOCKER_TESTS = (
    "tests/test_sandbox_security.py::test_container_overlay_extraction",
    "tests/test_sandbox_phase7_network.py::test_sandbox_network_deny_all",
    "tests/test_sandbox_phase7_network.py::test_sandbox_network_localhost_only",
    "tests/test_sandbox_phase8_secrets.py::test_secret_policy_docker_deny_all",
    "tests/test_sandbox_phase8_secrets.py::test_secret_policy_docker_allow_explicit",
    "tests/test_sandbox_phase8_secrets.py::test_docker_env_file_cleanup",
    "tests/test_sandbox_phase8_secrets.py::test_process_inheritance_isolation",
    "tests/test_sandbox_r2_security.py::test_docker_autocommit_uses_staged_changes",
    "tests/test_sandbox_resource_bombs.py::test_memory_exhaustion_bomb",
    "tests/test_sandbox_resource_bombs.py::test_storage_exhaustion_bomb",
    "tests/test_sandbox_lifecycle_recovery.py::test_reconciliation_cleans_orphaned_containers",
    "tests/test_sandbox_lifecycle_recovery.py::test_process_timeout_cleanup",
)


def run_live_docker_suite(report_path: Path) -> bool:
    docker = shutil.which("docker")
    if not docker:
        raise RuntimeError("Docker CLI is required for the live release suite.")
    info = subprocess.run(
        [docker, "info"], capture_output=True, text=True, timeout=30, check=False
    )
    if info.returncode != 0:
        raise RuntimeError("Docker daemon is unavailable for the live release suite.")
    report_path.parent.mkdir(parents=True, exist_ok=True)
    command = [
        sys.executable,
        "-m",
        "pytest",
        "-q",
        *LIVE_DOCKER_TESTS,
        f"--junitxml={report_path}",
    ]
    result = subprocess.run(command, text=True, timeout=600, check=False)
    if result.returncode != 0 or not report_path.is_file():
        return False
    root = ET.parse(report_path).getroot()
    skipped = sum(int(suite.get("skipped", "0")) for suite in root.iter("testsuite"))
    failures = sum(int(suite.get("failures", "0")) for suite in root.iter("testsuite"))
    errors = sum(int(suite.get("errors", "0")) for suite in root.iter("testsuite"))
    return skipped == 0 and failures == 0 and errors == 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("report", type=Path)
    args = parser.parse_args()
    try:
        passed = run_live_docker_suite(args.report)
    except (OSError, RuntimeError, subprocess.TimeoutExpired, ET.ParseError) as exc:
        print(f"Live Docker release suite failed: {exc}", file=sys.stderr)
        return 2
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
