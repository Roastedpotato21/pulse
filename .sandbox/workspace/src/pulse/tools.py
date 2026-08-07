"""Built-in Pulse tools. They depend on services, never on the CLI."""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
from typing import Any

from pulse.config import AgentConfig
from pulse.edits import EditWorkflow
from pulse.git import GitIntelligence
from pulse.memory import LongTermMemory
from pulse.mutations import MutationTracker
from pulse.provider import ModelProvider
from pulse.repository import RepositoryIndex
from pulse.tool_registry import ToolInvocation, ToolResult
from pulse.verification import VerificationEngine


class BaseTool:
    requires_permission = False

    def matches(self, invocation: ToolInvocation) -> bool:
        return invocation.name == self.name


class StatusTool(BaseTool):
    name = "status"
    description = "Show the active Pulse configuration."

    def __init__(self, config: AgentConfig, provider: ModelProvider) -> None:
        self.config, self.provider = config, provider

    async def execute(self, invocation: ToolInvocation) -> ToolResult:
        api_key = getattr(self.provider, "api_key_env_var", "Provider API key")
        content = "\n".join((
            f"Mode: {self.config.mode}", f"Provider: {self.config.model.provider}",
            f"Model: {self.config.model.name}", f"Writes enabled: {self.config.sandbox.allow_writes}",
            f"{api_key} present: {self.provider.is_configured}",
        ))
        return ToolResult(content, metadata={"config": self.config})


class DoctorTool(BaseTool):
    name = "doctor"
    description = "Check local Pulse configuration and provider readiness."

    def __init__(self, workspace: Path, config: AgentConfig, provider: ModelProvider) -> None:
        self.workspace, self.config, self.provider = workspace, config, provider

    async def execute(self, invocation: ToolInvocation) -> ToolResult:
        checks = {
            "workspace": self.workspace.exists(), "agent.config.json": (self.workspace / "agent.config.json").exists(),
            ".env": (self.workspace / ".env").exists(), "provider": self.provider.is_configured,
            "uv": shutil.which("uv") is not None, "model": bool(self.config.model.name),
        }
        content = "\n".join(f"{name}: {'OK' if ok else 'Needs attention'}" for name, ok in checks.items())
        return ToolResult(content, metadata={"checks": checks})


class MutationsTool(BaseTool):
    name = "mutations"
    description = "Show tracked workspace mutations."

    def __init__(self, mutations: MutationTracker) -> None:
        self.mutations = mutations

    async def execute(self, invocation: ToolInvocation) -> ToolResult:
        events = self.mutations.latest_transaction() if invocation.arguments.get("last") else list(self.mutations.history())
        if not events:
            return ToolResult("No tracked mutations found.", metadata={"events": []})
        content = "\n".join(
            f"{event.get('timestamp')} {event.get('action')} {event.get('file_path')}" for event in events
        )
        return ToolResult(content, metadata={"events": events})


class EditTool(BaseTool):
    name = "edit"
    description = "Show a proposed file diff and apply it only after approval."

    def __init__(self, edits: EditWorkflow, git: GitIntelligence | None = None) -> None:
        self.edits, self.git = edits, git

    async def execute(self, invocation: ToolInvocation) -> ToolResult:
        arguments = invocation.arguments
        approve = arguments.get("approve")
        if not callable(approve):
            raise TypeError("Edit requires an async approval handler.")
        before = await self.git.inspect() if self.git else None
        result = await self.edits.request_and_apply(
            str(arguments["file"]), str(arguments["content"]), str(arguments.get("reason", "Requested edit")), approve
        )
        after = await self.git.inspect() if self.git and result.applied else None
        suggestion = after.commit_suggestion if after else None
        content = "Edit applied." if result.applied else "Edit discarded."
        if suggestion:
            content += f" Suggested commit: {suggestion}"
        return ToolResult(
            content,
            metadata={"proposal": result.proposal, "applied": result.applied, "git_before": before, "git_after": after, "commit_suggestion": suggestion},
        )


class RollbackTool(BaseTool):
    name = "rollback"
    description = "Restore the last approved edit from its tracked snapshot."
    requires_permission = True

    def __init__(self, edits: EditWorkflow) -> None:
        self.edits = edits

    async def execute(self, invocation: ToolInvocation) -> ToolResult:
        rolled_back = await self.edits.rollback_last()
        return ToolResult("Last approved edit rolled back." if rolled_back else "No approved edit to roll back.")


class VerifyTool(BaseTool):
    name = "verify"
    description = "Detect and run the project's test suite."

    def __init__(self, verification: VerificationEngine) -> None:
        self.verification = verification

    def matches(self, invocation: ToolInvocation) -> bool:
        return super().matches(invocation) or invocation.message.strip().lower() in {"verify", "run tests", "test project"}

    async def execute(self, invocation: ToolInvocation) -> ToolResult:
        result = await self.verification.verify()
        if result.framework is None:
            return ToolResult(result.analysis, metadata={"verification": result})
        state = "passed" if result.success else "failed"
        content = f"{result.framework} verification {state} after {result.attempts} attempt(s).\n{result.analysis}"
        return ToolResult(content, metadata={"verification": result})


class GitTool(BaseTool):
    name = "git"
    description = "Show Git branch, status, diff summary, and a commit suggestion."

    def __init__(self, git: GitIntelligence) -> None:
        self.git = git

    def matches(self, invocation: ToolInvocation) -> bool:
        return super().matches(invocation) or invocation.message.strip().lower() in {"git status", "git", "show git status"}

    async def execute(self, invocation: ToolInvocation) -> ToolResult:
        insight = await self.git.inspect()
        if not insight.status.is_repository:
            return ToolResult("This workspace is not a Git repository.", metadata={"git": insight})
        branch = insight.status.branch or "detached HEAD"
        summary = f"Branch: {branch}\nHEAD: {insight.status.head or 'unborn'}\nChanges: {insight.diff.files_changed} files, +{insight.diff.additions}/-{insight.diff.deletions}"
        if insight.commit_suggestion:
            summary += f"\nSuggested commit: {insight.commit_suggestion}"
        return ToolResult(summary, metadata={"git": insight})


class MemoryTool(BaseTool):
    name = "memory"
    description = "Store preferences and inspect long-term project memory."

    def __init__(self, memory: LongTermMemory) -> None:
        self.memory = memory

    async def execute(self, invocation: ToolInvocation) -> ToolResult:
        arguments = invocation.arguments
        key, value = arguments.get("preference_key"), arguments.get("preference_value")
        if key is not None and value is not None:
            await self.memory.set_preference(str(key), str(value))
            return ToolResult(f"Remembered preference: {key}.")
        query = str(arguments.get("query", ""))
        preferences, entries = await asyncio.gather(self.memory.preferences(), self.memory.retrieve(query))
        lines = [f"Preference: {key} = {value}" for key, value in preferences.items()]
        lines.extend(f"{entry.category}: {entry.content}" for entry in entries)
        return ToolResult("\n".join(lines) or "No long-term memory stored yet.", metadata={"preferences": preferences, "entries": entries})


class IndexTool(BaseTool):
    name = "index"
    description = "Incrementally index repository files, folders, imports, and symbols."

    def __init__(self, repository: RepositoryIndex) -> None:
        self.repository = repository

    async def execute(self, invocation: ToolInvocation) -> ToolResult:
        report = await self.repository.index()
        return ToolResult(
            f"Indexed {report.files} files and {report.folders} folders ({report.indexed} changed, {report.unchanged} unchanged, {report.removed} removed).",
            metadata={"report": report},
        )


class SearchTool(BaseTool):
    name = "search"
    description = "Find repository files by filename and semantic terms."

    def __init__(self, repository: RepositoryIndex) -> None:
        self.repository = repository

    async def execute(self, invocation: ToolInvocation) -> ToolResult:
        query = str(invocation.arguments["query"])
        results = await self.repository.search(query)
        return ToolResult("\n".join(f"{result.path} (score {result.score:g})" for result in results) or "No matching files.", metadata={"results": results})


class SymbolsTool(BaseTool):
    name = "symbols"
    description = "List imports, classes, and functions from one indexed file."

    def __init__(self, repository: RepositoryIndex) -> None:
        self.repository = repository

    async def execute(self, invocation: ToolInvocation) -> ToolResult:
        file_path = str(invocation.arguments["file"])
        details = await self.repository.details(file_path)
        if not details:
            return ToolResult("No indexed file found.", metadata={"symbols": []})
        imports = [f"import {item}" for item in details.imports]
        symbols = [f"{symbol.kind} {symbol.name}:{symbol.line}" for symbol in details.symbols]
        return ToolResult("\n".join(imports + symbols) or "No symbols found.", metadata={"symbols": details.symbols, "imports": details.imports})


class TaskTool(BaseTool):
    name = "task"
    description = "Manage tasks: create, list, inspect, resume, or cancel tasks."

    def __init__(self, task_manager: Any) -> None:
        self.task_manager = task_manager

    def matches(self, invocation: ToolInvocation) -> bool:
        return invocation.name in {"task", "tasks", "resume", "cancel"}

    async def execute(self, invocation: ToolInvocation) -> ToolResult:
        cmd = invocation.name
        args = invocation.arguments

        if cmd == "tasks" or (cmd == "task" and not args.get("id") and not args.get("action")):
            status_filter = args.get("status")
            status_enum = None
            if status_filter:
                from pulse.task_manager import TaskStatus
                try:
                    status_enum = TaskStatus(str(status_filter).upper())
                except ValueError:
                    pass
            tasks = self.task_manager.list_tasks(status=status_enum)
            if not tasks:
                return ToolResult("No tasks found.", metadata={"tasks": []})
            lines = [f"{t.id} [{t.status.value}] ({t.priority.name}) {t.title} - {t.progress:.0f}%" for t in tasks]
            return ToolResult("\n".join(lines), metadata={"tasks": [t.to_dict() for t in tasks]})

        task_id = str(args.get("id", ""))
        action = str(args.get("action", cmd)).lower()

        if action in {"resume", "cancel", "show", "get", "task"}:
            if not task_id:
                return ToolResult("Task ID is required.")

            if action == "resume":
                try:
                    task = await self.task_manager.resume_task(task_id)
                    return ToolResult(f"Resumed task {task_id} [{task.status.value}].", metadata={"task": task.to_dict()})
                except ValueError as e:
                    return ToolResult(f"Error resuming task: {e}")
            elif action == "cancel":
                reason = str(args.get("reason", "CLI cancellation"))
                try:
                    task = await self.task_manager.cancel_task(task_id, reason=reason)
                    return ToolResult(f"Cancelled task {task_id} [{task.status.value}].", metadata={"task": task.to_dict()})
                except ValueError as e:
                    return ToolResult(f"Error cancelling task: {e}")
            else:
                task = self.task_manager.get_task(task_id)
                if not task:
                    return ToolResult(f"Task '{task_id}' not found.")
                info = (
                    f"ID: {task.id}\nTitle: {task.title}\nGoal: {task.goal}\n"
                    f"Status: {task.status.value}\nPriority: {task.priority.name}\n"
                    f"Progress: {task.progress:.1f}%\nRetries: {task.retries}/{task.max_retries}\n"
                    f"Checkpoints: {len(task.checkpoints)}\nCreated: {task.created_at}"
                )
                return ToolResult(info, metadata={"task": task.to_dict()})

        return ToolResult(f"Unknown task action: {action}")


class SessionTool(BaseTool):
    name = "session"
    description = "Manage sessions: list, inspect, resume, or archive sessions."

    def __init__(self, session_manager: Any) -> None:
        self.session_manager = session_manager

    def matches(self, invocation: ToolInvocation) -> bool:
        return invocation.name in {"session", "sessions", "resume-session"}

    async def execute(self, invocation: ToolInvocation) -> ToolResult:
        cmd = invocation.name
        args = invocation.arguments

        if cmd == "sessions":
            sessions = self.session_manager.store.list_all()
            if not sessions:
                return ToolResult("No sessions found.", metadata={"sessions": []})
            lines = [f"Session {s.id} ({s.title}) - Status: {s.status.value}" for s in sessions]
            return ToolResult("\n".join(lines), metadata={"sessions": [s.to_dict() for s in sessions]})

        if cmd == "session":
            session_id = str(args.get("id", ""))
            try:
                session = await self.session_manager.load_session(session_id)
                return ToolResult(f"Session: {session.title}\nStatus: {session.status.value}\nTasks: {len(session.active_tasks)}", metadata={"session": session.to_dict()})
            except ValueError as e:
                return ToolResult(str(e))

        if cmd == "resume-session":
            session_id = str(args.get("id", ""))
            try:
                session = await self.session_manager.resume_session(session_id)
                return ToolResult(f"Session {session_id} resumed.", metadata={"session": session.to_dict()})
            except ValueError as e:
                return ToolResult(str(e))

        return ToolResult(f"Unknown session command: {cmd}")
