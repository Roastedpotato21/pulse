import asyncio
from pathlib import Path

from pulse.verification import VerificationEngine


def test_detects_python_javascript_and_java_test_runners(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'sample'\n", encoding="utf-8")
    assert VerificationEngine(tmp_path).detect().framework == "pytest"

    (tmp_path / "pyproject.toml").unlink()
    (tmp_path / "package.json").write_text("{}", encoding="utf-8")
    assert VerificationEngine(tmp_path).detect().framework == "npm"

    (tmp_path / "package.json").unlink()
    (tmp_path / "pom.xml").write_text("<project />", encoding="utf-8")
    assert VerificationEngine(tmp_path).detect().framework == "maven"

    (tmp_path / "pom.xml").unlink()
    (tmp_path / "build.gradle").write_text("", encoding="utf-8")
    assert VerificationEngine(tmp_path).detect().framework == "gradle"


def test_verification_retries_approved_repairs_up_to_a_passing_result(tmp_path: Path) -> None:
    (tmp_path / "pytest.ini").write_text("[pytest]", encoding="utf-8")
    runs = []
    repairs = []

    async def runner(command, workspace):
        runs.append(command)
        return (1, "", "AssertionError: expected value") if len(runs) == 1 else (0, "1 passed", "")

    async def repair(result):
        repairs.append(result.analysis)
        return True

    result = asyncio.run(VerificationEngine(tmp_path, runner=runner).verify(repair=repair))

    assert result.success and result.attempts == 2 and result.repairs_attempted == 1
    assert repairs == ["AssertionError: expected value"]


def test_verification_stops_after_three_repair_attempts(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text("{}", encoding="utf-8")
    runs = 0

    async def runner(command, workspace):
        nonlocal runs
        runs += 1
        return 1, "FAIL test", ""

    async def repair(result):
        return True

    result = asyncio.run(VerificationEngine(tmp_path, runner=runner).verify(repair=repair))

    assert not result.success and result.attempts == 4 and result.repairs_attempted == 3 and runs == 4
