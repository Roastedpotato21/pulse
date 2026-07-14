import subprocess
import sys


def test_cli_module_entrypoint_shows_help() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "pulse.cli", "--help"],
        check=True,
        capture_output=True,
        text=True,
    )

    assert "Permissioned single-model project agent." in result.stdout
    assert "ask" in result.stdout
