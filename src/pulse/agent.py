from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from pulse.agent_manager import AgentManager
from pulse.audit import AuditLog
from pulse.cli_ui import print_answer, print_error, print_info, print_warning
from pulse.context import ContextManager
from pulse.core.agent import Agent, AgentRequest
from pulse.edits import EditProposal
from pulse.memory import LongTermMemory, MemoryContextSource
from pulse.provider import ChatMessage, ModelProvider
from pulse.repository import RepositoryIndex
from pulse.sandbox import ProjectSandbox
from pulse.tool_registry import ToolInvocation, ToolRegistry


@dataclass(frozen=True)
class FileContext:
    file: str
    content: str


@dataclass(frozen=True)
class GeneratedFile:
    path: str
    content: str


class ProjectAgent:
    def __init__(
        self,
        name: str,
        sandbox: ProjectSandbox,
        provider: ModelProvider,
        audit: AuditLog,
        tools: ToolRegistry | None = None,
        repository: RepositoryIndex | None = None,
        memory: LongTermMemory | None = None,
        manager: AgentManager | None = None,
        context_manager: ContextManager | None = None,
    ) -> None:
        self.name = name
        self.sandbox = sandbox
        self.provider = provider
        self.audit = audit
        self.repository = repository
        self.memory = memory
        self.manager = manager
        self.tools = tools
        self.context_manager = context_manager
        self._orchestrator = Agent(
            provider,
            system_prompt=(
                f"You are {name}, a local coding assistant with permission-gated workspace tools. "
                "For normal conversation, answer directly without using project files. "
                "For project work, use only approved project context and tool results. Never "
                "claim that you are remote or lack filesystem access: Pulse can inspect and edit "
                "the active workspace through its tools. Never claim a file was changed unless a "
                "tool result confirms it. If more context is needed, name the file or tool needed.\n\n"
                "When answering architecture, design, or overview questions, you must write a comprehensive and deeply detailed explanation based on the retrieved context files.\n"
                "You must organize your answer using exactly these 6 headings:\n"
                "1. Project overview\n2. Main components\n3. Data flow\n4. Agent workflow\n"
                "5. Key technologies\n6. Execution flow\n\n"
                "CRITICAL: Do not just output the headings. You MUST write at least one full paragraph of descriptive text under each heading, analyzing the project files. "
                "Do not repeat these instructions, and do not return raw file contents."
            ), context_source=MemoryContextSource(memory) if memory else None, tool_registry=tools,
        )

    def ask(
        self,
        question: str,
        *,
        auto_approve_reads: bool = False,
        conversation_id: str = "default",
        conversation_history: Sequence[tuple[str, str]] = (),
    ) -> str | None:
        if not question:
            print_warning("Please pass a question.")
            return None

        self.audit.record("question", ".", "Question received.")
        use_project_context = self._needs_project_context(question)
        project_files = self.sandbox.list_files() if use_project_context else []
        relevant_files: list[str] = []
        if use_project_context and self.repository:
            relevant = asyncio.run(self.repository.search(question))
            relevant_files = [result.path for result in relevant]
            project_files = relevant_files + [file for file in project_files if file not in relevant_files]
        context = (
            []
            if not use_project_context or self._is_file_listing_question(question)
            else self._collect_context(question, project_files, relevant_files, auto_approve_reads=auto_approve_reads)
        )

        if not self.provider.is_configured:
            self._print_local_answer(question, project_files, context)
            api_key_env_var = getattr(self.provider, "api_key_env_var", "the provider API key")
            print_warning(
                f"\nModel call skipped: run `pulse keys` to configure {api_key_env_var} securely."
            )
            return None

        self.audit.record("model-call", ".", f"Using {self.provider.config.provider}:{self.provider.config.name}.")
        try:
            approved_context = [f"File: {item.file}\n---\n{item.content}" for item in context]
            history_context = self._conversation_history_context(conversation_history)
            if history_context:
                approved_context.insert(0, history_context)
            if project_files and not approved_context:
                approved_context.append("Project files:\n" + "\n".join(f"- {file}" for file in project_files))

            # Prepend ranked, token-budgeted context from the ContextManager
            # so the model receives the most relevant signals first.
            if self.context_manager:
                managed = asyncio.run(self.context_manager.as_strings(question))
                approved_context = [*managed, *approved_context]

            matched_tool = tools_match(self.tools, question)
            if self.manager and not matched_tool and not self._is_architecture_question(question):
                response_content = asyncio.run(self.manager.run(question, approved_context)).final_response
            else:
                response_content = asyncio.run(
                    self._orchestrator.respond(
                        AgentRequest(
                            message=question,
                            conversation_id=conversation_id,
                            context=approved_context,
                        )
                    )
                ).content
            print_answer(response_content)
            if self.memory:
                asyncio.run(self.memory.remember_task(question, response_content))
            return response_content
        except RuntimeError as error:
            print_error(f"\nModel call failed: {error}")
            print_error(self._provider_recovery_hint(error))
            return None

    @staticmethod
    def should_create_workspace_files(question: str) -> bool:
        """Recognize direct build requests without treating tutorials as mutations."""
        normalized = " ".join(question.lower().split())
        if normalized.startswith(("how ", "explain ", "show me how")):
            return False
        creation_terms = (
            "create",
            "build",
            "make",
            "generate",
            "implement",
            "write",
            "scaffold",
            "set up",
        )
        artifact_terms = (
            "file",
            "folder",
            "directory",
            "page",
            "landing",
            "site",
            "website",
            "html",
            "css",
            "javascript",
            "component",
            "project",
            " app",
        )
        has_named_file = re.search(r"\b[\w.-]+\.(?:html?|css|js|ts|tsx|jsx|py|md|json|ya?ml)\b", normalized)
        return any(term in normalized for term in creation_terms) and (
            bool(has_named_file) or any(
            term in normalized for term in artifact_terms
            )
        )

    def plan_workspace_files(
        self,
        request: str,
        *,
        conversation_history: Sequence[tuple[str, str]] = (),
    ) -> list[GeneratedFile]:
        """Ask the model for a bounded workspace-relative file manifest."""
        if not self.provider.is_configured:
            raise RuntimeError("A configured model provider is required to create files.")
        history = self._conversation_history_context(conversation_history)
        messages: list[dict[str, str]] = [
            {
                "role": "system",
                "content": (
                    "You are Pulse's workspace file planner. Return only valid JSON with this "
                    "shape: {\"files\":[{\"path\":\"relative/path\",\"content\":\"complete file content\"}]}. "
                    "Create every text file needed for the request. Paths must be relative, must "
                    "not contain '..', and must stay inside the workspace. Do not use Markdown "
                    "fences, explanations, terminal commands, or placeholders."
                ),
            }
        ]
        if history:
            messages.append({"role": "system", "content": history})
        messages.append({"role": "user", "content": request})

        async def generate() -> str:
            chunks = [
                chunk.content
                async for chunk in self.provider.generate_stream(messages)
            ]
            return "".join(chunks)

        raw = asyncio.run(generate()).strip()
        try:
            start, end = raw.index("{"), raw.rindex("}") + 1
            payload = json.loads(raw[start:end])
        except (ValueError, json.JSONDecodeError) as error:
            raise RuntimeError(
                "The model did not return a valid workspace file plan. Try again."
            ) from error
        records = payload.get("files") if isinstance(payload, dict) else None
        if not isinstance(records, list) or not records or len(records) > 25:
            raise RuntimeError("The model returned an invalid number of workspace files.")

        files: list[GeneratedFile] = []
        seen_paths: set[str] = set()
        total_bytes = 0
        for record in records:
            if not isinstance(record, dict):
                raise TypeError("The model returned an invalid workspace file entry.")
            path, content = record.get("path"), record.get("content")
            if not isinstance(path, str) or not isinstance(content, str):
                raise TypeError("Every generated file requires a path and text content.")
            candidate = Path(path)
            normalized_parts = {part.lower() for part in candidate.parts}
            content_bytes = len(content.encode("utf-8"))
            if (
                not path.strip()
                or candidate.is_absolute()
                or ".." in candidate.parts
                or normalized_parts.intersection({".git", ".agent", ".pulse", ".venv"})
                or candidate.name.lower() in {".env", "credentials.json"}
                or len(path) > 240
                or content_bytes > 1_000_000
            ):
                raise RuntimeError("The model proposed an unsafe workspace file path or size.")
            normalized_path = candidate.as_posix().lower()
            if normalized_path in seen_paths:
                raise RuntimeError("The model proposed the same workspace file more than once.")
            seen_paths.add(normalized_path)
            total_bytes += content_bytes
            if total_bytes > 5_000_000:
                raise RuntimeError("The workspace file plan is too large to apply safely.")
            files.append(GeneratedFile(path=candidate.as_posix(), content=content))
        return files

    def apply_workspace_files(
        self,
        files: Sequence[GeneratedFile],
        approve: Callable[[EditProposal], bool],
    ) -> list[EditProposal]:
        """Apply generated files through the registered approval-gated edit tool."""
        if self.tools is None or self.tools.get("edit") is None:
            raise RuntimeError("The workspace edit tool is unavailable.")

        batch_id = str(uuid4())

        async def apply_one(file: GeneratedFile) -> EditProposal | None:
            async def approve_proposal(proposal: EditProposal) -> bool:
                return approve(proposal)

            result = await self.tools.execute(
                ToolInvocation(
                    name="edit",
                    arguments={
                        "file": file.path,
                        "content": file.content,
                        "reason": "Created from an interactive Pulse request.",
                        "approve": approve_proposal,
                        "batch_id": batch_id,
                    },
                    metadata={"detailed_edit_approval": True},
                )
            )
            if not result or not result.metadata.get("applied"):
                return None
            proposal = result.metadata.get("proposal")
            if not isinstance(proposal, EditProposal):
                return None
            written = self.sandbox.read_file_for_edit(proposal.file_path)
            if written != proposal.after_content:
                raise RuntimeError(f"Write verification failed for {proposal.file_path}.")
            return proposal

        applied: list[EditProposal] = []
        for file in files:
            proposal = asyncio.run(apply_one(file))
            if proposal:
                applied.append(proposal)
        return applied

    @staticmethod
    def _conversation_history_context(
        history: Sequence[tuple[str, str]],
    ) -> str:
        """Return a bounded transcript for the first turn after resuming a chat."""
        if not history:
            return ""
        remaining = 12_000
        selected: list[str] = []
        for role, content in reversed(history[-20:]):
            label = "User" if role == "user" else "Pulse"
            entry = f"{label}: {content.strip()}"
            if len(entry) > remaining:
                entry = entry[-remaining:]
            selected.append(entry)
            remaining -= len(entry)
            if remaining <= 0:
                break
        selected.reverse()
        return "Previous conversation (for continuity only):\n" + "\n\n".join(selected)

    async def respond_remote(self, prompt: str, context: list[str]) -> str:
        """Serve an IDE/client prompt without importing any transport concerns."""
        self.audit.record("remote-question", ".", "Remote question received.")

        # Prepend managed context (ranked, token-budgeted) from ContextManager
        # before caller-supplied context so the model sees the best signals first.
        if self.context_manager:
            managed = await self.context_manager.as_strings(prompt)
            context = [*managed, *context]

        matched_tool = tools_match(self.tools, prompt)
        if self.manager and not matched_tool and not self._is_architecture_question(prompt):
            memory_context = await self.memory.context_for(prompt) if self.memory else []
            response = (await self.manager.run(prompt, (*context, *memory_context))).final_response
        else:
            response = (await self._orchestrator.respond(AgentRequest(message=prompt, context=context))).content
        if self.memory:
            await self.memory.remember_task(prompt, response)
        return response

    def _collect_context(self, question: str, project_files: list[str], relevant_files: list[str] | None = None, *, auto_approve_reads: bool) -> list[FileContext]:
        selected = self._select_context_files(question, project_files, relevant_files or [])
        context: list[FileContext] = []

        for file in selected:
            content = self.sandbox.read_file(
                file,
                f"{self.name} wants to inspect this file for your question.",
                auto_approve=auto_approve_reads,
            )
            if content is not None:
                context.append(FileContext(file=file, content=content))

        return context

    def _is_architecture_question(self, question: str) -> bool:
        lower = question.lower()
        return any(term in lower for term in ("architecture", "design", "overview", "structure"))

    def _select_context_files(self, question: str, project_files: list[str], relevant_files: list[str] | None = None) -> list[str]:
        lower = question.lower()
        explicit = [file for file in project_files if file.lower() in lower]
        if explicit:
            return explicit[:6]
            
        if self._is_architecture_question(question):
            arch_files = {
                "README.md",
                "src/pulse/runtime.py",
                "src/pulse/agent.py",
                "src/pulse/agent_manager.py",
                "src/pulse/context.py",
                "src/pulse/reasoning.py",
            }
            results = [file for file in project_files if file.replace("\\", "/") in arch_files]
            if relevant_files:
                results.extend(file for file in relevant_files if file not in results)
            return results[:6]

        # The repository index is consulted before this point.  Prefer its
        # ranked candidates to a static starter set so project questions reach
        # the model with the files most likely to answer them.
        if relevant_files:
            return relevant_files[:6]

        useful = {
            "README.md",
            "pyproject.toml",
            "agent.config.json",
            ".gitignore",
            "src/pulse/cli.py",
        }
        return [file for file in project_files if file.replace("\\", "/") in useful][:6]

    def _needs_project_context(self, question: str) -> bool:
        lower = question.lower()
        project_terms = {
            "agent.config",
            "bug",
            "build",
            "change",
            "cli",
            "code",
            "debug",
            "error",
            "file",
            "fix",
            "implement",
            "project",
            "pyproject",
            "readme",
            "repo",
            "repository",
            "src/",
            "test",
            "traceback",
            "update",
        }
        return any(term in lower for term in project_terms) or self._is_architecture_question(question)

    def _is_file_listing_question(self, question: str) -> bool:
        lower = question.lower()
        return "file" in lower and any(term in lower for term in ("list", "show", "what files", "which files"))

    def _build_messages(self, question: str, context: list[FileContext], project_files: list[str] | None = None) -> list[ChatMessage]:
        file_blocks = "\n\n".join(f"File: {item.file}\n---\n{item.content}" for item in context)
        project_file_block = "\n".join(f"- {file}" for file in (project_files or []))
        context_block = file_blocks
        if project_file_block and not file_blocks:
            context_block = f"Project files:\n{project_file_block}"

        system = (
            f"You are {self.name}, a local coding assistant with permission-gated workspace tools. "
            "For normal conversation, answer directly without using project files. "
            "For project work, use only approved project context and tool results. Never claim "
            "that Pulse is remote or lacks filesystem access, and never claim an edit succeeded "
            "without a confirming tool result."
        )
        user = f"Question: {question}\n\n{context_block or 'No project context was requested or approved.'}"
        return [ChatMessage(role="system", content=system), ChatMessage(role="user", content=user)]

    def _print_local_answer(self, question: str, files: list[str], context: list[FileContext]) -> None:
        print_info(f"\n{self.name} local answer:")
        if "file" in question.lower():
            print_info("\n".join(f"- {file}" for file in files) if files else "No project files found.")
            return

        if not context:
            print_warning("No model is configured yet, so I can only answer project file-listing questions locally.")
            return

        read_files = ", ".join(item.file for item in context)
        api_key_env_var = getattr(self.provider, "api_key_env_var", "the provider API key")
        print_info(
            f"I read {read_files}. Run `pulse keys` to configure {api_key_env_var} securely."
        )

    def _provider_recovery_hint(self, error: RuntimeError) -> str:
        provider_name = self.provider.config.provider
        if provider_name == "openrouter":
            if "HTTP 402" in str(error) or "(402)" in str(error):
                return (
                    "OpenRouter accepted the API key but cannot charge this request. "
                    "Add credits or enable `pulse model auto openrouter` for a free model, "
                    "then retry."
                )
            if any(
                marker in str(error)
                for marker in ("HTTP 401", "HTTP 403", "(401)", "(403)")
            ):
                return "OpenRouter rejected the API key or model access. Check OPENROUTER_API_KEY and the selected model."
            return (
                "OpenRouter returned an error. Check your OpenRouter credits/billing and API key, "
                "or switch providers with `pulse model`."
            )
        return f"Check the {provider_name} API key, model name, and account status, then try again."


def tools_match(tools: ToolRegistry | None, message: str) -> bool:
    """Keep explicit/local tool requests on the established tool execution path."""
    if tools is None:
        return False
    from pulse.tool_registry import ToolInvocation

    return tools.match(ToolInvocation(message=message)) is not None
