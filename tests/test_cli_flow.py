from pathlib import Path
from types import SimpleNamespace

from pulse.agent import ProjectAgent
from pulse.audit import AuditLog
from pulse.config import SandboxConfig
from pulse.sandbox import ProjectSandbox


def test_agent_ask_can_auto_approve_reads_without_prompting(tmp_path: Path) -> None:
    workspace = tmp_path
    (workspace / "README.md").write_text("hello world", encoding="utf-8")
    sandbox = ProjectSandbox(
        SandboxConfig(
            workspace_root=workspace,
            require_permission_for_reads=True,
            require_permission_for_project_actions=False,
            allow_writes=False,
        ),
        AuditLog(workspace / ".agent" / "logs" / "actions.jsonl"),
    )
    provider = SimpleNamespace(is_configured=False, config=SimpleNamespace(provider="openrouter", name="test/model"))
    agent = ProjectAgent("Pulse", sandbox, provider, AuditLog(workspace / ".agent" / "logs" / "actions.jsonl"))

    agent.ask("What is in the file?", auto_approve_reads=True)


def test_agent_ask_does_not_touch_files_for_normal_conversation(tmp_path: Path) -> None:
    class ConversationOnlySandbox:
        def list_files(self):
            raise AssertionError("normal conversation should not list project files")

        def read_file(self, file, reason, auto_approve=False):
            raise AssertionError("normal conversation should not read project files")

    provider = SimpleNamespace(
        is_configured=True,
        config=SimpleNamespace(provider="openrouter", name="test/model"),
        chat=lambda messages: "Hello!",
    )
    agent = ProjectAgent("Pulse", ConversationOnlySandbox(), provider, AuditLog(tmp_path / ".agent" / "logs" / "actions.jsonl"))

    agent.ask("Hello, how are you?", auto_approve_reads=True)


def test_agent_ask_prints_model_failure_without_raising(tmp_path: Path, capsys) -> None:
    workspace = tmp_path
    (workspace / "README.md").write_text("hello world", encoding="utf-8")
    sandbox = ProjectSandbox(
        SandboxConfig(
            workspace_root=workspace,
            require_permission_for_reads=True,
            require_permission_for_project_actions=False,
            allow_writes=False,
        ),
        AuditLog(workspace / ".agent" / "logs" / "actions.jsonl"),
    )

    def fail_chat(messages):
        raise RuntimeError("Model request failed (402): Payment Required")

    provider = SimpleNamespace(
        is_configured=True,
        config=SimpleNamespace(provider="openrouter", name="test/model"),
        chat=fail_chat,
    )
    agent = ProjectAgent("Pulse", sandbox, provider, AuditLog(workspace / ".agent" / "logs" / "actions.jsonl"))

    agent.ask("What is in the file?", auto_approve_reads=True)

    output = capsys.readouterr().out
    assert "Model call failed: Model request failed (402): Payment Required" in output
    assert "accepted the API key but cannot charge this request" in output
