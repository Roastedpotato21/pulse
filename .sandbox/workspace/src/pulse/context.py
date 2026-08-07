"""Production-grade Context Manager for Pulse.

Architecture
============
``ContextManager`` gathers context from up to six built-in *source adapters*
and any number of user-registered adapters (RAG extension point).  Each source
returns :class:`ContextItem` objects that are ranked by relevance to the
current user request, then trimmed to a configurable token budget.

Two compression strategies are available:

* :class:`ContextCompressor` — heuristic head+tail truncation.  No extra LLM
  call; low latency; suitable when a model provider is not available.
* :class:`SummarizationCompressor` — LLM-backed summarisation.  Produces much
  denser summaries for large code/file items.  Falls back silently to the
  heuristic strategy when the provider is unavailable or raises.

Built-in sources (all optional via constructor injection):
    - :class:`ConversationHistorySource`    — recent conversation turns
    - :class:`RepositoryIntelligenceSource` — ranked file/symbol hits
    - :class:`MemorySource`                 — long-term memory & preferences
    - :class:`GitStatusSource`              — branch + working-tree state
    - :class:`ActiveFileSource`             — content of the IDE active file
    - :class:`UserIntentSource`             — intent signals parsed from prompt

Extension
=========
Any object that satisfies the :class:`ContextSource` structural protocol can
be registered at runtime::

    cm = ContextManager(...)
    await cm.register_source(MyRagSource())

The new source participates in every subsequent ``build()`` call.

Token Budget
============
Default budget is 6 000 tokens (≈ 24 000 characters).  Override via the
``max_tokens`` constructor parameter or the ``PULSE_CONTEXT_MAX_TOKENS``
environment variable.

Cache
=====
Built contexts are cached in-process for 30 s (configurable via ``cache_ttl``).
Call ``await cm.invalidate_cache(request)`` to force a refresh, e.g. between
autonomous loop turns where the repository or Git state may have changed.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ContextItem:
    """A single piece of context produced by a source adapter.

    Attributes:
        source: Human-readable source label (e.g. ``"memory"``, ``"git"``).
        content: Raw text content to be delivered to the LLM.
        relevance_score: Float in [0, 1] assigned by :class:`ContextRanker`.
        token_estimate: Approximate token count (1 token ≈ 4 characters).
        metadata: Arbitrary source-specific key/value pairs.
    """

    source: str
    content: str
    relevance_score: float = 0.0
    token_estimate: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.token_estimate:
            self.token_estimate = _estimate_tokens(self.content)


@dataclass(slots=True)
class BuiltContext:
    """Output of :meth:`ContextManager.build`.

    Attributes:
        items: Ranked, budget-fitted :class:`ContextItem` list (highest
            relevance first).
        total_tokens: Sum of ``item.token_estimate`` for included items.
        was_compressed: ``True`` if the compressor had to truncate any item.
        compression_strategy: ``"heuristic"`` or ``"llm"`` — which compressor ran.
        build_time_ms: Wall-clock milliseconds spent building this context.
    """

    items: list[ContextItem]
    total_tokens: int
    was_compressed: bool
    build_time_ms: float
    compression_strategy: str = "heuristic"


@dataclass(slots=True)
class ContextStats:
    """Diagnostic snapshot of a :class:`ContextManager` instance.

    Attributes:
        builtin_source_count: Number of built-in source adapters registered.
        extra_source_count: Number of user-registered (RAG) source adapters.
        cache_enabled: Whether the TTL cache is active.
        max_tokens: Configured token budget.
        compression_strategy: Active compression strategy name.
    """

    builtin_source_count: int
    extra_source_count: int
    cache_enabled: bool
    max_tokens: int
    compression_strategy: str


# ---------------------------------------------------------------------------
# Source protocol — RAG extension hook
# ---------------------------------------------------------------------------


class ContextSource(Protocol):
    """Structural protocol that every context source must satisfy.

    Any object with an ``async gather(request)`` method can be registered
    with :meth:`ContextManager.register_source` and will participate in
    every ``build()`` call.
    """

    async def gather(self, request: str) -> list[ContextItem]:
        """Gather context items relevant to *request*.

        Args:
            request: The raw user prompt / question.

        Returns:
            A list of :class:`ContextItem` objects.  The list may be empty.
        """
        ...


# ---------------------------------------------------------------------------
# Built-in source adapters
# ---------------------------------------------------------------------------


class ConversationHistorySource:
    """Injects recent conversation turns as context.

    Args:
        store: Any object with an async ``read(conversation_id)`` method that
            returns objects with ``role`` and ``content`` attributes.
        conversation_id: Identifier of the conversation to read.
        max_turns: Maximum number of recent turns to include.
    """

    def __init__(self, store: Any, conversation_id: str = "default", max_turns: int = 6) -> None:
        self._store = store
        self._conversation_id = conversation_id
        self._max_turns = max_turns

    async def gather(self, request: str) -> list[ContextItem]:
        try:
            messages = await self._store.read(self._conversation_id)
        # Intentionally broad to isolate execution boundaries and prevent crashes.
        except Exception:  # noqa: BLE001
            return []

        recent = messages[-self._max_turns * 2:]  # user + assistant pairs
        if not recent:
            return []

        lines = [f"{msg.role.capitalize()}: {msg.content}" for msg in recent]
        content = "Conversation history:\n" + "\n".join(lines)
        return [
            ContextItem(
                source="history",
                content=content,
                metadata={"turns": len(recent)},
            )
        ]


class RepositoryIntelligenceSource:
    """Retrieves ranked file/symbol hits from :class:`~pulse.repository.RepositoryIndex`.

    Args:
        repository: A ``RepositoryIndex`` instance (or duck-typed equivalent).
        limit: Maximum search results to include.
    """

    def __init__(self, repository: Any, limit: int = 5) -> None:
        self._repository = repository
        self._limit = limit

    async def gather(self, request: str) -> list[ContextItem]:
        try:
            results = await self._repository.search(request, limit=self._limit)
        # Intentionally broad to isolate execution boundaries and prevent crashes.
        except Exception:  # noqa: BLE001
            return []

        items: list[ContextItem] = []
        for result in results:
            symbols = ", ".join(f"{s.name}({s.kind})" for s in result.symbols[:8])
            content = f"Repository file: {result.path}"
            if symbols:
                content += f"\n  Symbols: {symbols}"
            items.append(
                ContextItem(
                    source="repository",
                    content=content,
                    relevance_score=min(result.score / 10.0, 1.0),
                    metadata={"path": result.path, "score": result.score},
                )
            )
        return items


class MemorySource:
    """Injects long-term memories and user preferences.

    Args:
        memory: A :class:`~pulse.memory.LongTermMemory` instance.
        limit: Maximum memory entries to retrieve.
    """

    def __init__(self, memory: Any, limit: int = 4) -> None:
        self._memory = memory
        self._limit = limit

    async def gather(self, request: str) -> list[ContextItem]:
        try:
            strings = await self._memory.context_for(request, limit=self._limit)
        # Intentionally broad to isolate execution boundaries and prevent crashes.
        except Exception:  # noqa: BLE001
            return []

        if not strings:
            return []

        content = "Long-term memory:\n" + "\n".join(f"- {s}" for s in strings)
        return [
            ContextItem(
                source="memory",
                content=content,
                metadata={"entries": len(strings)},
            )
        ]


class GitStatusSource:
    """Captures current Git branch and working-tree state.

    Args:
        git: A :class:`~pulse.git.GitIntelligence` instance.
    """

    def __init__(self, git: Any) -> None:
        self._git = git

    async def gather(self, request: str) -> list[ContextItem]:
        try:
            status = await self._git.status()
        # Intentionally broad to isolate execution boundaries and prevent crashes.
        except Exception:  # noqa: BLE001
            return []

        if not status.is_repository:
            return []

        lines = [f"Git branch: {status.branch or 'unknown'}"]
        if status.head:
            lines.append(f"HEAD: {status.head}")
        if status.changes:
            changed = [f"  {c.index_status}{c.worktree_status} {c.path}" for c in status.changes[:10]]
            lines.append("Changed files:")
            lines.extend(changed)
            if len(status.changes) > 10:
                lines.append(f"  … and {len(status.changes) - 10} more")

        content = "\n".join(lines)
        return [
            ContextItem(
                source="git",
                content=content,
                metadata={
                    "branch": status.branch,
                    "head": status.head,
                    "changes": len(status.changes),
                },
            )
        ]


class ActiveFileSource:
    """Injects the content (or a prefix) of the IDE's currently active file.

    Args:
        workspace: Root workspace path used to resolve relative paths.
        max_lines: Maximum lines to include from the file before truncation.
    """

    def __init__(self, workspace: Path, max_lines: int = 120) -> None:
        self._workspace = workspace
        self._max_lines = max_lines

    async def gather(self, request: str, *, active_file: str | None = None) -> list[ContextItem]:
        if not active_file:
            return []
        path = Path(active_file)
        if not path.is_absolute():
            path = self._workspace / path
        try:
            text = await asyncio.to_thread(path.read_text, encoding="utf-8", errors="replace")
        except OSError:
            return []

        lines = text.splitlines()
        truncated = len(lines) > self._max_lines
        snippet = "\n".join(lines[: self._max_lines])
        suffix = f"\n… ({len(lines) - self._max_lines} more lines)" if truncated else ""
        content = f"Active file: {path.name}\n```\n{snippet}{suffix}\n```"
        return [
            ContextItem(
                source="active_file",
                content=content,
                metadata={"path": str(path), "lines": len(lines), "truncated": truncated},
            )
        ]


class UserIntentSource:
    """Emits a concise intent summary derived from the user's prompt.

    Extracts key terms, verbs, and file/symbol references so the LLM has a
    clean, deduplicated statement of intent even when the raw prompt is long.

    Args:
        max_keywords: Maximum extracted keywords to surface.
    """

    # Action verbs that signal code-editing or investigative intent.
    _ACTION_VERBS = frozenset({
        "add", "build", "change", "check", "create", "debug", "delete",
        "explain", "find", "fix", "implement", "list", "modify", "refactor",
        "remove", "rename", "search", "show", "test", "update", "write",
    })

    def __init__(self, max_keywords: int = 10) -> None:
        self._max_keywords = max_keywords

    async def gather(self, request: str) -> list[ContextItem]:
        if not request.strip():
            return []

        words = re.findall(r"[a-zA-Z_][a-zA-Z0-9_./-]*", request.lower())
        actions = [w for w in words if w in self._ACTION_VERBS]
        # File/symbol references: tokens containing "." or "/" or ending in .py etc.
        refs = [w for w in words if ("." in w or "/" in w) and len(w) > 2]
        # General keywords: longer words, deduped, excluding stop words.
        stop = {"the", "a", "an", "in", "on", "at", "to", "for", "of", "and",
                 "or", "is", "it", "be", "as", "by", "that", "this", "with"}
        keywords = list(dict.fromkeys(
            w for w in words if len(w) > 3 and w not in stop
        ))[: self._max_keywords]

        summary_parts: list[str] = []
        if actions:
            summary_parts.append(f"Intent actions: {', '.join(dict.fromkeys(actions))}")
        if refs:
            summary_parts.append(f"File/symbol refs: {', '.join(refs[:6])}")
        if keywords:
            summary_parts.append(f"Key terms: {', '.join(keywords)}")

        if not summary_parts:
            return []

        content = "User intent analysis:\n" + "\n".join(summary_parts)
        return [
            ContextItem(
                source="intent",
                content=content,
                metadata={"actions": actions, "refs": refs, "keywords": keywords},
            )
        ]


# ---------------------------------------------------------------------------
# Ranker
# ---------------------------------------------------------------------------


class ContextRanker:
    """Scores each :class:`ContextItem` by relevance to the user request.

    Uses a TF-IDF-style keyword overlap between the request and item content.
    Repository items receive a boost from their pre-computed ``score`` field.
    Memory and history items receive a small recency boost so they surface
    even when keyword overlap is low.

    Args:
        boost_repository: Extra multiplier applied to repository item scores.
        boost_memory: Additive boost applied to memory/history items.
    """

    def __init__(self, boost_repository: float = 1.5, boost_memory: float = 0.05) -> None:
        self._boost_repository = boost_repository
        self._boost_memory = boost_memory

    def rank(self, items: list[ContextItem], request: str) -> list[ContextItem]:
        """Return *items* sorted highest-relevance first with updated scores.

        Args:
            items: Raw items from all sources.
            request: The user's prompt.

        Returns:
            A new list of :class:`ContextItem` with ``relevance_score`` filled.
        """
        if not request.strip():
            return items

        query_terms = self._terms(request)
        if not query_terms:
            return items

        scored: list[ContextItem] = []
        for item in items:
            item_terms = self._terms(item.content)
            overlap = len(query_terms.intersection(item_terms))
            score = overlap / max(len(query_terms), 1)

            # Apply source-specific boosts.
            if item.source == "repository":
                score = score * self._boost_repository + item.relevance_score * 0.3
            elif item.source in {"memory", "history"}:
                score = score + self._boost_memory
            elif item.source == "intent":
                # Intent is always surfaced near the top.
                score = max(score, 0.5)

            score = min(score, 1.0)
            scored.append(
                ContextItem(
                    source=item.source,
                    content=item.content,
                    relevance_score=round(score, 4),
                    token_estimate=item.token_estimate,
                    metadata=item.metadata,
                )
            )

        return sorted(scored, key=lambda i: (-i.relevance_score, i.source))

    @staticmethod
    def _terms(text: str) -> set[str]:
        words = re.findall(r"[a-zA-Z_][a-zA-Z0-9_]*", text.lower())
        return {w for w in words if len(w) > 2}


# ---------------------------------------------------------------------------
# Compressors
# ---------------------------------------------------------------------------

_COMPRESSION_KEEP_HEAD = 30  # lines to keep from the start of an item
_COMPRESSION_KEEP_TAIL = 10  # lines to keep from the end of an item

# Prompt template used by SummarizationCompressor.
_SUMMARIZATION_PROMPT = (
    "You are a precise technical summarizer for a software engineering assistant.\n"
    "Summarize the following context item into at most {max_tokens} tokens, "
    "preserving all file names, symbol names, function signatures, and "
    "important design decisions.  Output only the summary, no preamble.\n\n"
    "Context item (source: {source}):\n{content}"
)


class ContextCompressor:
    """Heuristic compressor that truncates over-budget items.

    No external LLM call is made.  The compressor preserves the *head* and
    *tail* of each item's content on the assumption that file headers and
    trailing summaries are the most information-dense parts.

    Swap this out for :class:`SummarizationCompressor` to get LLM-backed
    summarisation at the cost of an extra API call.

    Args:
        max_tokens: Token budget for the total context.
        head_lines: Lines to keep from the start of each truncated item.
        tail_lines: Lines to keep from the end of each truncated item.
    """

    name: str = "heuristic"

    def __init__(
        self,
        max_tokens: int = 6000,
        head_lines: int = _COMPRESSION_KEEP_HEAD,
        tail_lines: int = _COMPRESSION_KEEP_TAIL,
    ) -> None:
        self._max_tokens = max_tokens
        self._head_lines = head_lines
        self._tail_lines = tail_lines

    async def compress(self, items: list[ContextItem]) -> tuple[list[ContextItem], bool]:
        """Fit *items* into the token budget.

        First, items with zero relevance are dropped.  Then the least-relevant
        items are removed until the budget is satisfied.  Finally, if the budget
        is still exceeded, individual items are truncated using the head/tail
        strategy.

        Args:
            items: Ranked list of :class:`ContextItem` objects.

        Returns:
            A ``(fitted_items, was_compressed)`` tuple.
        """
        was_compressed = False

        # Drop zero-relevance items first (intent always scores >= 0.5 so safe).
        fitted = [i for i in items if i.relevance_score > 0.0 or i.source == "intent"]
        if not fitted:
            fitted = list(items)  # nothing to drop; keep all

        # Remove lowest-ranked items until budget is satisfied.
        while fitted and sum(i.token_estimate for i in fitted) > self._max_tokens:
            fitted.pop()  # items are highest-first; pop removes lowest
            was_compressed = True

        # If still over budget (a single item is very large), truncate items.
        for index, item in enumerate(fitted):
            if sum(i.token_estimate for i in fitted) <= self._max_tokens:
                break
            truncated_content = self._truncate(item.content)
            if truncated_content != item.content:
                was_compressed = True
                fitted[index] = ContextItem(
                    source=item.source,
                    content=truncated_content,
                    relevance_score=item.relevance_score,
                    token_estimate=_estimate_tokens(truncated_content),
                    metadata={**item.metadata, "compressed": True},
                )

        return fitted, was_compressed

    def _truncate(self, text: str) -> str:
        lines = text.splitlines()
        if len(lines) <= self._head_lines + self._tail_lines:
            return text
        omitted = len(lines) - self._head_lines - self._tail_lines
        head = lines[: self._head_lines]
        tail = lines[-self._tail_lines:]
        return "\n".join(head) + f"\n… [{omitted} lines omitted] …\n" + "\n".join(tail)


class SummarizationCompressor:
    """LLM-backed compressor that summarises over-budget context items.

    When the ranked items exceed *max_tokens*, this compressor calls the
    provided LLM provider to produce a dense, accurate summary of each
    over-budget item.  It falls back silently to the heuristic strategy
    if the provider is unavailable or raises.

    This compressor satisfies the same interface as :class:`ContextCompressor`
    and can be swapped in via the ``compressor`` parameter of
    :class:`ContextManager`.

    Args:
        provider: Any object with a ``chat(messages, temperature)`` method
            (satisfies :class:`~pulse.core.protocols.LLMProvider`).
        max_tokens: Token budget for the total context.
        summary_tokens_per_item: Maximum tokens allowed per summarised item.
        fallback: Heuristic compressor used when the provider is unavailable.

    Example::

        from pulse.context import ContextManager, SummarizationCompressor
        from pulse.provider import OpenAIProvider

        provider = OpenAIProvider(config, workspace / ".env")
        compressor = SummarizationCompressor(provider=provider, max_tokens=6000)
        cm = ContextManager(..., compressor=compressor)
    """

    name: str = "llm"

    def __init__(
        self,
        provider: Any,
        max_tokens: int = 6000,
        summary_tokens_per_item: int = 300,
        fallback: ContextCompressor | None = None,
    ) -> None:
        self._provider = provider
        self._max_tokens = max_tokens
        self._summary_tokens = summary_tokens_per_item
        self._fallback = fallback or ContextCompressor(max_tokens=max_tokens)

    async def compress(self, items: list[ContextItem]) -> tuple[list[ContextItem], bool]:
        """Summarise over-budget items using the LLM, then apply heuristic fallback.

        Items that already fit in the budget are returned unchanged.  Only
        items that cause a budget overflow are candidates for summarisation.
        If the provider raises, the heuristic strategy is used for that item.

        Args:
            items: Ranked list of :class:`ContextItem` objects.

        Returns:
            A ``(fitted_items, was_compressed)`` tuple.
        """
        was_compressed = False
        total = sum(i.token_estimate for i in items)

        if total <= self._max_tokens:
            return list(items), False

        # Summarise all items except intent/git (structural, already tiny).
        summarised: list[ContextItem] = []
        for item in items:
            if item.source in {"intent", "git"} or item.token_estimate <= self._summary_tokens:
                summarised.append(item)
                continue
            try:
                summary_text = await self._summarize(item)
                was_compressed = True
                summarised.append(
                    ContextItem(
                        source=item.source,
                        content=summary_text,
                        relevance_score=item.relevance_score,
                        token_estimate=_estimate_tokens(summary_text),
                        metadata={**item.metadata, "compressed": True, "compression": "llm_summary"},
                    )
                )
            # Intentionally broad to isolate execution boundaries and prevent crashes.
            except Exception:  # noqa: BLE001
                # Provider unavailable — fall back to the heuristic for this item.
                summarised.append(item)

        # After summarisation, apply heuristic to handle any remaining overflow.
        fitted, heuristic_compressed = await self._fallback.compress(summarised)
        return fitted, was_compressed or heuristic_compressed

    async def _summarize(self, item: ContextItem) -> str:
        """Call the LLM provider to summarise a single context item."""
        prompt = _SUMMARIZATION_PROMPT.format(
            max_tokens=self._summary_tokens,
            source=item.source,
            content=item.content[: self._summary_tokens * 8],  # rough char limit
        )
        messages = [{"role": "user", "content": prompt}]
        # Use chat() (sync) if the provider doesn't expose async, but prefer
        # the async path via asyncio.to_thread to stay non-blocking.
        if hasattr(self._provider, "_chat"):
            return await self._provider._chat(messages, temperature=0.1)
        return await asyncio.to_thread(self._provider.chat, messages, 0.1)


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _CacheEntry:
    result: BuiltContext
    expires_at: float


class _ContextCache:
    """Simple in-process TTL cache for built contexts."""

    def __init__(self, ttl_seconds: float = 30.0) -> None:
        self._ttl = ttl_seconds
        self._store: dict[str, _CacheEntry] = {}
        self._lock = asyncio.Lock()

    async def get(self, key: str) -> BuiltContext | None:
        async with self._lock:
            entry = self._store.get(key)
            if entry and time.monotonic() < entry.expires_at:
                return entry.result
            if entry:
                del self._store[key]
            return None

    async def set(self, key: str, value: BuiltContext) -> None:
        async with self._lock:
            self._store[key] = _CacheEntry(
                result=value,
                expires_at=time.monotonic() + self._ttl,
            )

    async def invalidate(self, key: str) -> None:
        async with self._lock:
            self._store.pop(key, None)

    async def clear(self) -> None:
        async with self._lock:
            self._store.clear()


# ---------------------------------------------------------------------------
# ContextManager
# ---------------------------------------------------------------------------


class ContextManager:
    """Async, provider-agnostic Context Manager for Pulse.

    Assembles ranked, compressed context from multiple sources and delivers it
    as a ``list[str]`` ready to be passed to any LLM provider.

    Args:
        memory: Optional :class:`~pulse.memory.LongTermMemory` instance.
        repository: Optional :class:`~pulse.repository.RepositoryIndex` instance.
        git: Optional :class:`~pulse.git.GitIntelligence` instance.
        workspace: Workspace root path (used by :class:`ActiveFileSource`).
        conversation_store: Optional conversation store for history injection.
        conversation_id: Conversation to read history from.
        max_tokens: Token budget for the assembled context.  Defaults to the
            ``PULSE_CONTEXT_MAX_TOKENS`` env var if set, otherwise 6 000.
        cache_ttl: TTL in seconds for the in-process cache (0 disables caching).
        ranker: Custom :class:`ContextRanker` (defaults to built-in).
        compressor: Custom compressor — either :class:`ContextCompressor`
            (default, heuristic) or :class:`SummarizationCompressor` (LLM-backed).

    Example::

        from pulse.context import ContextManager, SummarizationCompressor
        from pulse.memory import LongTermMemory
        from pulse.repository import RepositoryIndex
        from pulse.git import GitIntelligence

        cm = ContextManager(
            memory=LongTermMemory(workspace),
            repository=RepositoryIndex(workspace),
            git=GitIntelligence(workspace),
            workspace=workspace,
            # Use LLM-backed summarisation for large projects:
            compressor=SummarizationCompressor(provider=provider),
        )

        strings = await cm.as_strings("How does the planner work?")
        # Pass `strings` as context to any LLM call.
    """

    def __init__(
        self,
        *,
        memory: Any | None = None,
        repository: Any | None = None,
        git: Any | None = None,
        workspace: Path | None = None,
        conversation_store: Any | None = None,
        conversation_id: str = "default",
        max_tokens: int | None = None,
        cache_ttl: float = 30.0,
        ranker: ContextRanker | None = None,
        compressor: ContextCompressor | SummarizationCompressor | None = None,
    ) -> None:
        self._max_tokens = max_tokens or int(os.environ.get("PULSE_CONTEXT_MAX_TOKENS", "6000"))
        self._ranker = ranker or ContextRanker()
        self._compressor: ContextCompressor | SummarizationCompressor = (
            compressor or ContextCompressor(max_tokens=self._max_tokens)
        )
        self._cache = _ContextCache(ttl_seconds=cache_ttl) if cache_ttl > 0 else None

        # Built-in sources (registered in priority order).
        self._builtin_sources: list[Any] = []
        if conversation_store:
            self._builtin_sources.append(
                ConversationHistorySource(conversation_store, conversation_id)
            )
        if memory:
            self._builtin_sources.append(MemorySource(memory))
        if repository:
            self._builtin_sources.append(RepositoryIntelligenceSource(repository))
        if git:
            self._builtin_sources.append(GitStatusSource(git))
        if workspace:
            self._builtin_sources.append(ActiveFileSource(workspace))
        self._builtin_sources.append(UserIntentSource())

        # Extra (user-registered) sources — RAG extension point.
        self._extra_sources: list[Any] = []

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def register_source(self, source: ContextSource) -> None:
        """Register an additional :class:`ContextSource` at runtime.

        The source participates in every subsequent :meth:`build` call.
        Useful for plugging in RAG retrieval, vector databases, or any other
        external context provider.

        Args:
            source: Any object satisfying the :class:`ContextSource` protocol.
        """
        self._extra_sources.append(source)

    async def build(
        self,
        request: str,
        *,
        active_file: str | None = None,
    ) -> BuiltContext:
        """Build ranked, compressed context for *request*.

        Results are cached for ``cache_ttl`` seconds.  Pass ``active_file`` to
        inject the currently open IDE file.

        Args:
            request: The raw user prompt / question.
            active_file: Optional path to the IDE's active file.

        Returns:
            A :class:`BuiltContext` ready for LLM consumption.
        """
        cache_key = _cache_key(request, active_file)
        if self._cache:
            cached = await self._cache.get(cache_key)
            if cached is not None:
                return cached

        start = time.monotonic()
        items = await self._gather_all(request, active_file=active_file)
        ranked = self._ranker.rank(items, request)
        fitted, was_compressed = await self._compressor.compress(ranked)
        total_tokens = sum(i.token_estimate for i in fitted)
        result = BuiltContext(
            items=fitted,
            total_tokens=total_tokens,
            was_compressed=was_compressed,
            build_time_ms=round((time.monotonic() - start) * 1000, 2),
            compression_strategy=self._compressor.name,
        )

        if self._cache:
            await self._cache.set(cache_key, result)

        return result

    async def as_strings(
        self,
        request: str,
        *,
        active_file: str | None = None,
    ) -> list[str]:
        """Return assembled context as a plain ``list[str]``.

        This is the primary integration point for :class:`AgentOrchestrator`,
        :class:`AgentManager`, :class:`AutonomousLoop`, and
        :class:`ProjectAgent`.

        Args:
            request: The raw user prompt / question.
            active_file: Optional path to the IDE's active file.

        Returns:
            Ordered list of context strings (highest relevance first).
        """
        built = await self.build(request, active_file=active_file)
        return [item.content for item in built.items]

    async def invalidate_cache(self, request: str, *, active_file: str | None = None) -> None:
        """Invalidate the cached context for the given request.

        Call this between autonomous loop turns or after repository/Git state
        changes to force a fresh context build on the next :meth:`build` call.

        Args:
            request: The prompt whose cache entry should be evicted.
            active_file: Active file used in the original ``build()`` call.
        """
        if self._cache:
            await self._cache.invalidate(_cache_key(request, active_file))

    async def clear_cache(self) -> None:
        """Evict all cached context entries.

        Use when you know the workspace state has changed broadly (e.g. after
        a large Git rebase or file system restructure).
        """
        if self._cache:
            await self._cache.clear()

    def stats(self) -> ContextStats:
        """Return a diagnostic snapshot of this :class:`ContextManager`.

        Returns:
            A :class:`ContextStats` dataclass with source counts, cache status,
            token budget, and active compression strategy.
        """
        return ContextStats(
            builtin_source_count=len(self._builtin_sources),
            extra_source_count=len(self._extra_sources),
            cache_enabled=self._cache is not None,
            max_tokens=self._max_tokens,
            compression_strategy=self._compressor.name,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _gather_all(
        self, request: str, *, active_file: str | None = None
    ) -> list[ContextItem]:
        """Gather items from all sources concurrently."""
        all_sources = [*self._builtin_sources, *self._extra_sources]

        async def _gather_one(source: Any) -> list[ContextItem]:
            try:
                # ActiveFileSource.gather() accepts an optional active_file kwarg.
                if isinstance(source, ActiveFileSource):
                    return await source.gather(request, active_file=active_file)
                return await source.gather(request)
            # Intentionally broad to isolate execution boundaries and prevent crashes.
            except Exception:  # noqa: BLE001
                # Never let a single failing source break the whole build.
                return []

        results: list[list[ContextItem]] = await asyncio.gather(
            *(_gather_one(src) for src in all_sources)
        )
        # Flatten while preserving source order.
        return [item for batch in results for item in batch]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _estimate_tokens(text: str) -> int:
    """Estimate token count using the rule-of-thumb: 1 token ≈ 4 characters."""
    return max(1, len(text) // 4)


def _cache_key(request: str, active_file: str | None) -> str:
    raw = f"{request}|{active_file or ''}"
    return hashlib.sha256(raw.encode()).hexdigest()
