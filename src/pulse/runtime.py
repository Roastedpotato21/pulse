from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from pulse.agent import ProjectAgent
from pulse.audit import AuditLog
from pulse.config import AgentConfig, load_agent_config
from pulse.provider import ModelProvider, ProviderFactory
from pulse.sandbox import ProjectSandbox


@dataclass(slots=True)
class AgentRuntime:
    workspace: Path
    config: AgentConfig
    audit: AuditLog
    sandbox: ProjectSandbox
    provider: ModelProvider
    agent: ProjectAgent


def build_runtime(workspace: Path, config: AgentConfig | None = None) -> AgentRuntime:
    resolved_workspace = workspace.resolve()
    resolved_config = config or load_agent_config(resolved_workspace)
    audit = AuditLog(resolved_config.logging.action_log)
    sandbox = ProjectSandbox(resolved_config.sandbox, audit)
    provider = ProviderFactory().create(resolved_config.model.provider, resolved_config.model, resolved_workspace / ".env")
    agent = ProjectAgent(resolved_config.agent_name, sandbox, provider, audit)

    return AgentRuntime(
        workspace=resolved_workspace,
        config=resolved_config,
        audit=audit,
        sandbox=sandbox,
        provider=provider,
        agent=agent,
    )
