from __future__ import annotations

import subprocess
from pathlib import Path

from scripts.run_docker_release_tests import LIVE_DOCKER_TESTS, run_live_docker_suite


def test_docker_release_gate_rejects_skipped_security_tests(
    tmp_path: Path, monkeypatch
) -> None:
    report = tmp_path / "docker.xml"
    calls = 0

    def fake_run(command, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return subprocess.CompletedProcess(command, 0, "ok", "")
        report.write_text(
            '<testsuites><testsuite tests="1" failures="0" errors="0" skipped="1"/></testsuites>',
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr("scripts.run_docker_release_tests.shutil.which", lambda _: "docker")
    monkeypatch.setattr("scripts.run_docker_release_tests.subprocess.run", fake_run)

    assert run_live_docker_suite(report) is False
    assert len(LIVE_DOCKER_TESTS) >= 10
