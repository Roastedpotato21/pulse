import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from pulse.audit import AuditLog
from pulse.config import SandboxConfig
from pulse.sandbox import ProjectSandbox


class SandboxTests(unittest.TestCase):
    def test_workspace_escape_is_blocked(self) -> None:
        with TemporaryDirectory() as directory:
            tmp_path = Path(directory)
            audit = AuditLog(tmp_path / ".agent" / "logs" / "actions.jsonl")
            sandbox = ProjectSandbox(
                SandboxConfig(
                    workspace_root=tmp_path,
                    require_permission_for_reads=False,
                    require_permission_for_project_actions=True,
                    allow_writes=False,
                ),
                audit,
            )

            with self.assertRaisesRegex(ValueError, "outside workspace"):
                sandbox.read_file("../outside.txt", "test")


if __name__ == "__main__":
    unittest.main()
