import asyncio
from pathlib import Path
from types import SimpleNamespace

from pulse.agent import ProjectAgent
from pulse.audit import AuditLog
from pulse.config import SandboxConfig
from pulse.memory import LongTermMemory
from pulse.repository import RepositoryIndex
from pulse.sandbox import ProjectSandbox


async def successful_stream(messages):
    yield SimpleNamespace(content="Hello!", metadata={})


async def failing_stream(messages):
    raise RuntimeError("Model request failed (402): Payment Required")
    yield SimpleNamespace(content="", metadata={})


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
        generate_stream=successful_stream,
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

    provider = SimpleNamespace(
        is_configured=True,
        config=SimpleNamespace(provider="openrouter", name="test/model"),
        generate_stream=failing_stream,
    )
    agent = ProjectAgent("Pulse", sandbox, provider, AuditLog(workspace / ".agent" / "logs" / "actions.jsonl"))

    agent.ask("What is in the file?", auto_approve_reads=True)

    output = capsys.readouterr().out
    assert "Model call failed: Model request failed (402): Payment Required" in output
    assert "accepted the API key but cannot charge this request" in output


def test_agent_uses_repository_search_results_as_pre_llm_context(tmp_path: Path) -> None:
    (tmp_path / "invoice_service.py").write_text("def calculate_invoice(): return 42\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("general project notes", encoding="utf-8")
    sandbox = ProjectSandbox(
        SandboxConfig(
            workspace_root=tmp_path,
            require_permission_for_reads=False,
            require_permission_for_project_actions=False,
            allow_writes=False,
        ),
        AuditLog(tmp_path / ".agent" / "logs" / "actions.jsonl"),
    )
    received_messages = []

    async def recording_stream(messages):
        received_messages.extend(messages)
        yield SimpleNamespace(content="Found it.", metadata={})

    provider = SimpleNamespace(
        is_configured=True,
        config=SimpleNamespace(provider="openrouter", name="test/model"),
        generate_stream=recording_stream,
    )
    agent = ProjectAgent(
        "Pulse", sandbox, provider, AuditLog(tmp_path / ".agent" / "logs" / "actions.jsonl"),
        repository=RepositoryIndex(tmp_path),
    )

    agent.ask("Explain the invoice code", auto_approve_reads=True)

    context = "\n".join(message["content"] for message in received_messages)
    assert "File: invoice_service.py" in context


def test_agent_supplies_long_term_memory_before_model_call(tmp_path: Path) -> None:
    memory = LongTermMemory(tmp_path)
    asyncio.run(memory.store_project_context("The preferred formatter is ruff.", tags=("formatter",)))
    received_messages = []

    async def recording_stream(messages):
        received_messages.extend(messages)
        yield SimpleNamespace(content="Noted.", metadata={})

    provider = SimpleNamespace(
        is_configured=True,
        config=SimpleNamespace(provider="openrouter", name="test/model"),
        generate_stream=recording_stream,
    )
    agent = ProjectAgent(
        "Pulse", SimpleNamespace(), provider, AuditLog(tmp_path / ".agent" / "logs" / "actions.jsonl"), memory=memory,
    )

    agent.ask("What formatter should I use?", auto_approve_reads=True)

    assert any("preferred formatter is ruff" in message["content"] for message in received_messages)
