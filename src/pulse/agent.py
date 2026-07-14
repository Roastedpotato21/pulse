from __future__ import annotations

from dataclasses import dataclass

from pulse.audit import AuditLog
from pulse.provider import ChatMessage, ModelProvider
from pulse.sandbox import ProjectSandbox


@dataclass(frozen=True)
class FileContext:
    file: str
    content: str


class ProjectAgent:
    def __init__(self, name: str, sandbox: ProjectSandbox, provider: ModelProvider, audit: AuditLog) -> None:
        self.name = name
        self.sandbox = sandbox
        self.provider = provider
        self.audit = audit

    def ask(self, question: str, *, auto_approve_reads: bool = False) -> None:
        if not question:
            print("Please pass a question.")
            return

        self.audit.record("question", ".", f"Asked: {question}")
        use_project_context = self._needs_project_context(question)
        project_files = self.sandbox.list_files() if use_project_context else []
        context = (
            []
            if not use_project_context or self._is_file_listing_question(question)
            else self._collect_context(question, project_files, auto_approve_reads=auto_approve_reads)
        )

        if not self.provider.is_configured:
            self._print_local_answer(question, project_files, context)
            api_key_env_var = getattr(self.provider, "api_key_env_var", "the provider API key")
            print(f"\nModel call skipped: set {api_key_env_var} in .env to enable model-backed answers.")
            return

        self.audit.record("model-call", ".", f"Using {self.provider.config.provider}:{self.provider.config.name}.")
        try:
            print(self.provider.chat(self._build_messages(question, context, project_files)))
        except RuntimeError as error:
            print(f"\nModel call failed: {error}")
            print(self._provider_recovery_hint(error))

    def _collect_context(self, question: str, project_files: list[str], *, auto_approve_reads: bool) -> list[FileContext]:
        selected = self._select_context_files(question, project_files)
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

    def _select_context_files(self, question: str, project_files: list[str]) -> list[str]:
        lower = question.lower()
        explicit = [file for file in project_files if file.lower() in lower]
        if explicit:
            return explicit[:6]

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
        return any(term in lower for term in project_terms)

    def _is_file_listing_question(self, question: str) -> bool:
        lower = question.lower()
        return "file" in lower and any(term in lower for term in {"list", "show", "what files", "which files"})

    def _build_messages(self, question: str, context: list[FileContext], project_files: list[str] | None = None) -> list[ChatMessage]:
        file_blocks = "\n\n".join(f"File: {item.file}\n---\n{item.content}" for item in context)
        project_file_block = "\n".join(f"- {file}" for file in (project_files or []))
        context_block = file_blocks
        if project_file_block and not file_blocks:
            context_block = f"Project files:\n{project_file_block}"

        system = (
            f"You are {self.name}, a single-model, read-only project assistant. "
            "For normal conversation, answer directly without using project files. "
            "For project work, use only approved project context. If more context is needed, say which file should be approved next."
        )
        user = f"Question: {question}\n\n{context_block or 'No project context was requested or approved.'}"
        return [ChatMessage(role="system", content=system), ChatMessage(role="user", content=user)]

    def _print_local_answer(self, question: str, files: list[str], context: list[FileContext]) -> None:
        print(f"\n{self.name} local answer:")
        if "file" in question.lower():
            print("\n".join(f"- {file}" for file in files) if files else "No project files found.")
            return

        if not context:
            print("No model is configured yet, so I can only answer project file-listing questions locally.")
            return

        read_files = ", ".join(item.file for item in context)
        api_key_env_var = getattr(self.provider, "api_key_env_var", "the provider API key")
        print(f"I read {read_files}. Add {api_key_env_var} to .env for model-backed answers.")

    def _provider_recovery_hint(self, error: RuntimeError) -> str:
        provider_name = self.provider.config.provider
        if provider_name == "openrouter":
            if "(402)" in str(error):
                return (
                    "OpenRouter accepted the API key but cannot charge this request. "
                    "Add credits or use a model your OpenRouter account can access, then retry."
                )
            if "(401)" in str(error) or "(403)" in str(error):
                return "OpenRouter rejected the API key or model access. Check OPENROUTER_API_KEY and the selected model."
            return (
                "OpenRouter returned an error. Check your OpenRouter credits/billing and API key, "
                "or switch AGENT_PROVIDER/AGENT_MODEL in .env."
            )
        return f"Check the {provider_name} API key, model name, and account status, then try again."
