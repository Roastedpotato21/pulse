from __future__ import annotations

import re
from typing import Any

from pulse.context import ContextManager
from pulse.core.agent import Agent, AgentRequest, AgentResponse
from pulse.core.protocols import LLMProvider
from pulse.repository import RepositoryIndex
from pulse.safety.safety_manager import SafetyManager
from pulse.tool_registry import ToolInvocation, ToolRegistry


class AgentOrchestrator:
    """Intercepts user prompts, queries Repository Intelligence, and manages tool/LLM execution flow.

    - Intercepts user prompt.
    - Runs Repository Intelligence.
    - Executes tool directly if intent is deterministic (e.g. read file, search symbol, registered tool).
    - Forwards to LLM only if intent is complex.
    """

    def __init__(
        self,
        agent: Agent | None = None,
        provider: LLMProvider | None = None,
        repository: RepositoryIndex | None = None,
        tool_registry: ToolRegistry | None = None,
        safety_manager: SafetyManager | None = None,
        system_prompt: str = "You are a pulse project assistant.",
        context_manager: ContextManager | None = None,
    ) -> None:
        self.repository = repository
        self.tool_registry = tool_registry or ToolRegistry()
        self.safety_manager = safety_manager or SafetyManager()
        self.context_manager = context_manager

        if agent:
            self.agent = agent
        elif provider:
            self.agent = Agent(
                provider=provider,
                system_prompt=system_prompt,
                tool_registry=self.tool_registry,
            )
        else:
            self.agent = None

    async def handle_request(self, request: AgentRequest | str) -> AgentResponse:
        if isinstance(request, str):
            request = AgentRequest(message=request)

        prompt = request.message.strip()

        # 1. Run Repository Intelligence
        repo_results = []
        if self.repository:
            repo_results = await self.repository.search(prompt)

        # 2. Execute tool directly if intent is deterministic
        deterministic_response = await self._try_deterministic_execution(prompt, repo_results, request)
        if deterministic_response is not None:
            return deterministic_response

        # 3. Forward to LLM only if intent is complex
        if not self.agent:
            return AgentResponse(
                content="No LLM agent or provider configured for handling complex prompts.",
                conversation_id=request.conversation_id,
                request_id=str(request.metadata.get("request_id", "local")),
            )

        context = list(request.context)

        # Prepend ContextManager output (ranked, compressed, token-budgeted)
        # before raw repository results so the LLM sees the most relevant
        # context first.
        if self.context_manager:
            managed_context = await self.context_manager.as_strings(prompt)
            context = [*managed_context, *context]

        if repo_results:
            repo_context = "Repository Intelligence Context:\n" + "\n".join(
                f"- {item.path} (score: {item.score})" for item in repo_results
            )
            context.append(repo_context)

        enriched_request = AgentRequest(
            message=request.message,
            conversation_id=request.conversation_id,
            context=context,
            metadata=request.metadata,
        )

        return await self.agent.respond(enriched_request)

    async def _try_deterministic_execution(
        self, prompt: str, repo_results: list[Any], request: AgentRequest
    ) -> AgentResponse | None:
        prompt_lower = prompt.lower()
        request_id = str(request.metadata.get("request_id", "deterministic"))

        # Check explicit tool registry match
        invocation = ToolInvocation(message=prompt, metadata=request.metadata)
        matched_tool = self.tool_registry.match(invocation)
        if matched_tool:
            authorized = await self.safety_manager.authorize(
                action=matched_tool.name,
                target=prompt,
                detail=f"Deterministic tool execution: {matched_tool.name}",
            )
            if not authorized:
                return AgentResponse(
                    content=f"Execution blocked by SafetyManager for tool: {matched_tool.name}",
                    conversation_id=request.conversation_id,
                    request_id=request_id,
                    tool_name=matched_tool.name,
                )

            tool_result = await self.tool_registry.execute(invocation)
            content = tool_result.content if tool_result else "Tool executed."
            return AgentResponse(
                content=content,
                conversation_id=request.conversation_id,
                request_id=request_id,
                tool_name=matched_tool.name,
            )

        # Deterministic symbol search intent
        if self.repository and any(kw in prompt_lower for kw in ["search symbol", "find symbol", "lookup symbol"]):
            symbol_query = re.sub(r"(?i)^(search|find|lookup)\s+symbol\s*", "", prompt).strip()
            if symbol_query:
                matches = await self.repository.search(symbol_query)
                found_symbols = []
                for m in matches:
                    for s in m.symbols:
                        if symbol_query.lower() in s.name.lower():
                            found_symbols.append(f"- `{s.name}` ({s.kind}) in `{m.path}` at line {s.line}")
                content = (
                    "Symbol search results:\n" + "\n".join(found_symbols)
                    if found_symbols
                    else f"No symbols matching '{symbol_query}' found."
                )
                return AgentResponse(
                    content=content,
                    conversation_id=request.conversation_id,
                    request_id=request_id,
                    tool_name="repository_symbol_search",
                )

        # Deterministic file read / listing intent
        if self.repository and ("list files" in prompt_lower or "show files" in prompt_lower):
            files = await self.repository.files()
            content = "Repository files:\n" + "\n".join(f"- {f}" for f in files)
            return AgentResponse(
                content=content,
                conversation_id=request.conversation_id,
                request_id=request_id,
                tool_name="repository_list_files",
            )

        return None
