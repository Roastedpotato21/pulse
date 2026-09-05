import asyncio
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

from pulse.agent import GeneratedFile, ProjectAgent
from pulse.audit import AuditLog
from pulse.config import SandboxConfig
from pulse.edits import EditProposal, EditWorkflow
from pulse.memory import LongTermMemory
from pulse.repository import RepositoryIndex
from pulse.sandbox import ProjectSandbox
from pulse.tool_policy import ToolPolicyEngine
from pulse.tool_registry import ToolRegistry
from pulse.tools import EditTool


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

    response = agent.ask("Hello, how are you?", auto_approve_reads=True)

    assert response == "Hello!"


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


def test_agent_receives_resumed_conversation_context(tmp_path: Path) -> None:
    received_messages = []

    async def recording_stream(messages):
        received_messages.extend(messages)
        yield SimpleNamespace(content="Continuing.", metadata={})

    provider = SimpleNamespace(
        is_configured=True,
        config=SimpleNamespace(provider="openrouter", name="test/model"),
        generate_stream=recording_stream,
    )
    agent = ProjectAgent(
        "Pulse",
        SimpleNamespace(),
        provider,
        AuditLog(tmp_path / ".agent" / "logs" / "actions.jsonl"),
    )

    agent.ask(
        "Continue that idea",
        conversation_id="restored-chat",
        conversation_history=(
            ("user", "Design an authentication flow"),
            ("assistant", "Start with PKCE."),
        ),
    )

    context = "\n".join(message["content"] for message in received_messages)
    assert "Previous conversation (for continuity only)" in context
    assert "User: Design an authentication flow" in context
    assert "Pulse: Start with PKCE." in context


def test_direct_creation_request_is_distinct_from_a_tutorial_question() -> None:
    assert ProjectAgent.should_create_workspace_files(
        "Create a folder and build a shoe landing page with HTML"
    )
    assert not ProjectAgent.should_create_workspace_files(
        "How do I create an HTML landing page?"
    )
    assert ProjectAgent.should_create_workspace_files(
        "create a folder and inside make a cake store landing page"
    )
    assert ProjectAgent.should_create_workspace_files("write index.html for a portfolio")


def test_agent_plans_workspace_files_from_model_json(tmp_path: Path) -> None:
    async def file_plan_stream(_messages):
        yield SimpleNamespace(
            content='{"files":[{"path":"shoe/index.html","content":"<h1>Shoes</h1>"}]}',
            metadata={},
        )

    provider = SimpleNamespace(
        is_configured=True,
        config=SimpleNamespace(provider="gemini", name="test/model"),
        generate_stream=file_plan_stream,
    )
    agent = ProjectAgent(
        "Pulse",
        SimpleNamespace(),
        provider,
        AuditLog(tmp_path / ".agent" / "logs" / "actions.jsonl"),
    )

    files = agent.plan_workspace_files("Create a shoe landing page")

    assert files == [GeneratedFile("shoe/index.html", "<h1>Shoes</h1>")]


def test_generated_file_uses_edit_tool_and_requires_detailed_approval(
    tmp_path: Path,
) -> None:
    sandbox = ProjectSandbox(
        SandboxConfig(
            workspace_root=tmp_path,
            require_permission_for_reads=False,
            require_permission_for_project_actions=True,
            allow_writes=False,
        ),
        AuditLog(tmp_path / ".agent" / "logs" / "actions.jsonl"),
    )

    async def permission_checker(invocation, _tool):
        assert invocation.metadata["detailed_edit_approval"] is True
        return True

    tools = ToolRegistry(
        [EditTool(EditWorkflow(sandbox))],
        permission_checker=permission_checker,
        policy_engine=ToolPolicyEngine(
            workspace=tmp_path,
            allowed_capabilities=frozenset({"edit"}),
        ),
    )
    provider = SimpleNamespace(is_configured=False)
    agent = ProjectAgent(
        "Pulse",
        sandbox,
        provider,
        AuditLog(tmp_path / ".agent" / "logs" / "actions.jsonl"),
        tools=tools,
    )

    approved_paths: list[str] = []
    applied = agent.apply_workspace_files(
        [GeneratedFile("shoe/index.html", "<h1>Shoes</h1>")],
        lambda proposal: approved_paths.append(proposal.file_path) or True,
    )

    assert [proposal.file_path for proposal in applied] == ["shoe/index.html"]
    assert approved_paths == ["shoe/index.html"]
    assert (tmp_path / "shoe" / "index.html").read_text(encoding="utf-8") == (
        "<h1>Shoes</h1>"
    )


def test_interactive_creation_request_uses_file_tools_instead_of_prose(
    tmp_path: Path, monkeypatch
) -> None:
    from pulse import cli

    class FakePrompt:
        def __init__(self, *_args, **_kwargs) -> None:
            self.inputs = iter(
                ["create a folder and inside make a cake store landing page", "/exit"]
            )

        def read(self) -> str:
            return next(self.inputs)

    class FakeAgent:
        def __init__(self) -> None:
            self.planned = False
            self.applied = False

        def should_create_workspace_files(self, question: str) -> bool:
            return ProjectAgent.should_create_workspace_files(question)

        def plan_workspace_files(self, *_args, **_kwargs):
            self.planned = True
            return [GeneratedFile("cake-store/index.html", "<h1>Cake Store</h1>")]

        def apply_workspace_files(self, *_args, **_kwargs):
            self.applied = True
            return [
                EditProposal(
                    "cake-store/index.html",
                    None,
                    "<h1>Cake Store</h1>",
                    "create page",
                    "--- a/cake-store/index.html\n+++ b/cake-store/index.html\n+<h1>Cake Store</h1>\n",
                )
            ]

        def ask(self, *_args, **_kwargs):
            raise AssertionError("A creation request must not fall back to prose.")

    fake_agent = FakeAgent()
    runtime = SimpleNamespace(
        agent=fake_agent,
        provider=SimpleNamespace(
            config=SimpleNamespace(provider="gemini", name="gemini-test")
        ),
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "InteractivePrompt", FakePrompt)
    monkeypatch.setattr(cli, "is_authenticated", lambda: True)
    monkeypatch.setattr(cli, "get_current_user", lambda: None)
    monkeypatch.setattr(cli, "_active_provider_has_key", lambda _workspace: True)
    monkeypatch.setattr(cli, "thinking_spinner", nullcontext)

    cli._handle_interactive_mode(None, fake_agent, runtime=runtime)

    assert fake_agent.planned is True
    assert fake_agent.applied is True
