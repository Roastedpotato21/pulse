from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from pulse.agent import ProjectAgent
from pulse.agent_manager import (
    AgentManager,
    DocumentationAgent,
    GitAgent,
    PlannerAgent,
    ReviewerAgent,
    SoftwareEngineerAgent,
    TesterAgent,
)
from pulse.audit import AuditLog
from pulse.auth import AuthenticationManager
from pulse.config import AgentConfig, load_agent_config
from pulse.context import ContextManager
from pulse.core.planner import RequestPlanner
from pulse.edits import EditWorkflow
from pulse.git import GitIntelligence
from pulse.memory import LongTermMemory
from pulse.mutations import MutationTracker
from pulse.provider import ModelProvider
from pulse.providers.manager import ProviderManager
from pulse.reasoning import ReasoningEngine
from pulse.repository import RepositoryIndex
from pulse.sandbox import ProjectSandbox
from pulse.session_manager import SessionManager
from pulse.software_engineer import AutonomousSoftwareEngineer
from pulse.streaming import StreamingExecutionEngine
from pulse.task_manager import TaskManager
from pulse.telemetry import TelemetryLogger
from pulse.tool_policy import ToolPolicyEngine
from pulse.tool_registry import ToolInvocation, ToolRegistry
from pulse.tools import (
    DoctorTool,
    EditTool,
    GitTool,
    IndexTool,
    MemoryTool,
    MutationsTool,
    RollbackTool,
    SearchTool,
    SessionTool,
    StatusTool,
    SymbolsTool,
    TaskTool,
    VerifyTool,
)
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
    task_manager: TaskManager
    session_manager: SessionManager
    context_manager: ContextManager
    reasoning_engine: ReasoningEngine
    planner: RequestPlanner
    streaming_engine: StreamingExecutionEngine
    software_engineer: AutonomousSoftwareEngineer
    auth: AuthenticationManager
    telemetry: TelemetryLogger


def build_runtime(workspace: Path, config: AgentConfig | None = None) -> AgentRuntime:
    resolved_workspace = workspace.resolve()
    resolved_config = config or load_agent_config(resolved_workspace)
    audit = AuditLog(resolved_config.logging.action_log)
    telemetry = TelemetryLogger(resolved_config.logging.telemetry_log)
    mutations = MutationTracker(resolved_workspace)
    sandbox = ProjectSandbox(resolved_config.sandbox, audit, mutations)

    provider_manager = ProviderManager(resolved_workspace)
    provider = provider_manager.create_provider(
        resolved_config.model, resolved_workspace / ".env"
    )
    audit.add_secret(getattr(provider, "api_key", None))
    telemetry.add_secret(getattr(provider, "api_key", None))

    edits = EditWorkflow(sandbox)
    repository = RepositoryIndex(resolved_workspace)
    verification = VerificationEngine(resolved_workspace)
    git = GitIntelligence(resolved_workspace)
    memory = LongTermMemory(
        resolved_workspace,
        secrets=[provider.api_key] if getattr(provider, "api_key", None) else None,
    )
    task_manager = TaskManager(resolved_workspace, memory=memory, telemetry=telemetry)
    session_manager = SessionManager(
        resolved_workspace, task_manager=task_manager, telemetry=telemetry
    )

    async def check_permission(invocation: ToolInvocation, tool: object) -> bool:
        return sandbox.request_project_action(
            "run tool", getattr(tool, "name", "tool"), "This action changes the project."
        )

    tool_capabilities = frozenset(
        {
            "status", "doctor", "mutations", "edit", "rollback", "git", "memory",
            "index", "search", "symbols", "verify", "task", "tasks", "resume", "cancel",
            "session", "sessions", "resume-session",
        }
    )
    tools = ToolRegistry(
        [
            StatusTool(resolved_config, provider), DoctorTool(resolved_workspace, resolved_config, provider),
            MutationsTool(mutations), EditTool(edits, git), RollbackTool(edits), GitTool(git), MemoryTool(memory),
        ],
        permission_checker=check_permission,
        telemetry=telemetry,
        policy_engine=ToolPolicyEngine(
            workspace=resolved_workspace,
            allowed_capabilities=tool_capabilities,
            audit_log=audit,
        ),
    )
    tools.register(IndexTool(repository))
    tools.register(SearchTool(repository))
    tools.register(SymbolsTool(repository))
    tools.register(VerifyTool(verification))
    tools.register(TaskTool(task_manager))
    tools.register(SessionTool(session_manager))

    manager = AgentManager(
        task_manager=task_manager,
        agents=[
            PlannerAgent(provider, tools),
            SoftwareEngineerAgent(provider, tools),
            ReviewerAgent(provider, tools),
            TesterAgent(provider, tools),
            DocumentationAgent(provider, tools),
            GitAgent(provider, tools),
        ],
    )
    agent = ProjectAgent(
        resolved_config.agent_name,
        sandbox,
        provider,
        audit,
        tools,
        repository,
        memory,
        manager,
    )

    context_manager = ContextManager(
        repository=repository, memory=memory, git=git, workspace=resolved_workspace
    )
    reasoning_engine = ReasoningEngine(
        provider=provider, context_manager=context_manager, tool_registry=tools
    )
    planner = RequestPlanner()
    streaming_engine = StreamingExecutionEngine(
        provider=provider,
        tool_registry=tools,
        reasoning_engine=reasoning_engine,
        task_manager=task_manager,
        telemetry=telemetry,
        verification_engine=verification,
    )
    software_engineer = AutonomousSoftwareEngineer(
        reasoning_engine=reasoning_engine,
        planner=planner,
        task_manager=task_manager,
        session_manager=session_manager,
        streaming_engine=streaming_engine,
        verification_engine=verification,
        context_manager=context_manager,
        memory=memory,
        repository=repository,
        tool_registry=tools,
    )
    auth = AuthenticationManager(resolved_workspace)

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
        task_manager=task_manager,
        session_manager=session_manager,
        context_manager=context_manager,
        reasoning_engine=reasoning_engine,
        planner=planner,
        streaming_engine=streaming_engine,
        software_engineer=software_engineer,
        auth=auth,
        telemetry=telemetry,
    )
