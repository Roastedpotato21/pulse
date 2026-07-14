from pathlib import Path

from pulse.config import AgentConfig, LoggingConfig, ModelConfig, SandboxConfig
from pulse.provider import GeminiProvider
from pulse.runtime import AgentRuntime, build_runtime


def test_build_runtime_composes_dependencies(tmp_path: Path) -> None:
    config = AgentConfig(
        agent_name="TestAgent",
        mode="single-model",
        model=ModelConfig(provider="openrouter", name="test/model", temperature=0.1),
        sandbox=SandboxConfig(
            workspace_root=tmp_path,
            require_permission_for_reads=False,
            require_permission_for_project_actions=False,
            allow_writes=False,
        ),
        logging=LoggingConfig(action_log=tmp_path / ".agent" / "logs" / "actions.jsonl"),
    )

    runtime = build_runtime(tmp_path, config)

    assert isinstance(runtime, AgentRuntime)
    assert runtime.workspace == tmp_path
    assert runtime.agent.name == "TestAgent"
    assert runtime.sandbox.config.workspace_root == tmp_path
    assert runtime.provider.config.name == "test/model"


def test_build_runtime_uses_configured_provider(tmp_path: Path) -> None:
    config = AgentConfig(
        agent_name="TestAgent",
        mode="single-model",
        model=ModelConfig(provider="gemini", name="gemini-2.0", temperature=0.1),
        sandbox=SandboxConfig(
            workspace_root=tmp_path,
            require_permission_for_reads=False,
            require_permission_for_project_actions=False,
            allow_writes=False,
        ),
        logging=LoggingConfig(action_log=tmp_path / ".agent" / "logs" / "actions.jsonl"),
    )

    runtime = build_runtime(tmp_path, config)

    assert isinstance(runtime.provider, GeminiProvider)
