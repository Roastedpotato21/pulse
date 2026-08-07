import os
import subprocess
import sys
from pathlib import Path


def test_cli_module_entrypoint_shows_help() -> None:
    env = os.environ.copy()
    src_path = Path(__file__).parent.parent / "src"
    env["PYTHONPATH"] = str(src_path) + os.pathsep + env.get("PYTHONPATH", "")

    result = subprocess.run(
        [sys.executable, "-m", "pulse.cli", "--help"],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )

    assert "Permissioned single-model project agent." in result.stdout
    assert "ask" in result.stdout
