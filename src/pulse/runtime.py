from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from pulse.agent import ProjectAgent
from pulse.audit import AuditLog
from pulse.config import AgentConfig, load_agent_config
from pulse.edits import EditWorkflow
from pulse.git import GitIntelligence
from pulse.mutations import MutationTracker
from pulse.memory import LongTermMemory
from pulse.multi_agent import AgentManager
from pulse.provider import ModelProvider, ProviderFactory
from pulse.repository import RepositoryIndex
from pulse.sandbox import ProjectSandbox
from pulse.tool_registry import ToolInvocation, ToolRegistry
from pulse.tools import DoctorTool, EditTool, GitTool, IndexTool, MemoryTool, MutationsTool, RollbackTool, SearchTool, StatusTool, SymbolsTool
from pulse.tools import VerifyTool
from pulse.verification import VerificationEngine


@dataclass(slots=True)
class AgentRuntime:
    workspace: Path
    config: AgentConfig
    audit: AuditLog
    mutations: MutationTracker
    sandbox: ProjectSandbox
    provider: ModelProvider
    agent: ProjectAgent
    edits: EditWorkflow
    tools: ToolRegistry
    repository: RepositoryIndex
    verification: VerificationEngine
    git: GitIntelligence
    memory: LongTermMemory
    manager: AgentManager


def build_runtime(workspace: Path, config: AgentConfig | None = None) -> AgentRuntime:
    resolved_workspace = workspace.resolve()
    resolved_config = config or load_agent_config(resolved_workspace)
    audit = AuditLog(resolved_config.logging.action_log)
    mutations = MutationTracker(resolved_workspace)
    sandbox = ProjectSandbox(resolved_config.sandbox, audit, mutations)
    provider = ProviderFactory().create(resolved_config.model.provider, resolved_config.model, resolved_workspace / ".env")
    edits = EditWorkflow(sandbox)
    repository = RepositoryIndex(resolved_workspace)
    verification = VerificationEngine(resolved_workspace)
    git = GitIntelligence(resolved_workspace)
    memory = LongTermMemory(resolved_workspace)
    manager = AgentManager(provider)
    async def check_permission(invocation: ToolInvocation, tool: object) -> bool:
        return sandbox.request_project_action("run tool", getattr(tool, "name", "tool"), "This action changes the project.")
    tools = ToolRegistry(
        [StatusTool(resolved_config, provider), DoctorTool(resolved_workspace, resolved_config, provider),
         MutationsTool(mutations), EditTool(edits, git), RollbackTool(edits), GitTool(git), MemoryTool(memory)],
        permission_checker=check_permission,
    )
    tools.register(IndexTool(repository))
    tools.register(SearchTool(repository))
    tools.register(SymbolsTool(repository))
    tools.register(VerifyTool(verification))
    agent = ProjectAgent(resolved_config.agent_name, sandbox, provider, audit, tools, repository, memory, manager)

    return AgentRuntime(
        workspace=resolved_workspace,
        config=resolved_config,
        audit=audit,
        mutations=mutations,
        sandbox=sandbox,
        provider=provider,
        agent=agent,
        edits=edits,
        tools=tools,
        repository=repository,
        verification=verification,
        git=git,
        memory=memory,
        manager=manager,
    )
