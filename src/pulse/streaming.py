"""Production-grade Streaming Execution Engine for Pulse.

Provides token-by-token LLM streaming, real-time tool execution progress,
structured event broadcasts (reasoning, planning, verification, task status),
cancellation token interrupts, and RPC / Telemetry / TaskManager integration.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import Any

from pulse.core.protocols import LLMProvider, StreamChunk
from pulse.reasoning import ReasoningEngine, ReasoningResult
from pulse.safety.safety_manager import SafetyManager
from pulse.sandbox.secrets import SecretScrubber
from pulse.tool_registry import ToolInvocation, ToolRegistry, ToolResult

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Enums & Data Models
# ---------------------------------------------------------------------------


class StreamEventType(Enum):
    """Event types emitted during streaming execution."""

    REASONING_START = "reasoning_start"
    REASONING_STEP = "reasoning_step"
    PLANNING_START = "planning_start"
    PLANNING_STEP = "planning_step"
    LLM_TOKEN = "llm_token"
    TOOL_START = "tool_start"
    TOOL_PROGRESS = "tool_progress"
    TOOL_COMPLETE = "tool_complete"
    TOOL_FAILED = "tool_failed"
    VERIFICATION_START = "verification_start"
    VERIFICATION_COMPLETE = "verification_complete"
    TASK_PROGRESS = "task_progress"
    COMPLETION = "completion"
    CANCELLED = "cancelled"
    ERROR = "error"


@dataclass(slots=True)
class StreamEvent:
    """A single structured streaming event emitted to clients."""

    event_type: StreamEventType
    timestamp: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    content: str = ""
    delta: str = ""
    step_number: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Serialize event to a JSON-compatible dictionary."""
        return {
            "event_type": self.event_type.value,
            "timestamp": self.timestamp,
            "content": self.content,
            "delta": self.delta,
            "step_number": self.step_number,
            "metadata": self.metadata,
        }


class CancellationToken:
    """Thread- and async-safe token used to signal stream cancellation."""

    def __init__(self) -> None:
        self._is_cancelled = False
        self._reason = ""

    @property
    def is_cancelled(self) -> bool:
        return self._is_cancelled

    @property
    def reason(self) -> str:
        return self._reason

    def cancel(self, reason: str = "Execution cancelled by user") -> None:
        self._is_cancelled = True
        self._reason = reason

    def raise_if_cancelled(self) -> None:
        if self._is_cancelled:
            raise asyncio.CancelledError(self._reason or "Execution cancelled")


# ---------------------------------------------------------------------------
# Streaming Execution Engine
# ---------------------------------------------------------------------------


class StreamingExecutionEngine:
    """Core async streaming engine for Pulse.

    Coordinates ReasoningEngine, TaskManager, LLM token streaming, Tool execution,
    Verification, Telemetry, and CancellationTokens in a unified event stream.
    """

    def __init__(
        self,
        *,
        reasoning_engine: ReasoningEngine | None = None,
        provider: LLMProvider | Any | None = None,
        orchestrator: Any | None = None,
        task_manager: Any | None = None,
        telemetry: Any | None = None,
        verification_engine: Any | None = None,
        safety_manager: SafetyManager | None = None,
        tool_registry: ToolRegistry | None = None,
    ) -> None:
        self.reasoning_engine = reasoning_engine or ReasoningEngine(
            provider=provider, safety_manager=safety_manager, tool_registry=tool_registry
        )
        self.provider = provider or getattr(self.reasoning_engine, "provider", None)
        self.orchestrator = orchestrator
        self.task_manager = task_manager
        self.telemetry = telemetry
        self.verification_engine = verification_engine
        self.safety_manager = safety_manager or getattr(self.reasoning_engine, "safety_manager", None)
        self.tool_registry = tool_registry or getattr(self.reasoning_engine, "tool_registry", None)
        self._scrubber = SecretScrubber(
            [str(provider.api_key)]
            if provider is not None and getattr(provider, "api_key", None)
            else None
        )

    async def execute_stream(
        self,
        request: str,
        *,
        active_file: str | None = None,
        task_id: str | None = None,
        cancellation_token: CancellationToken | None = None,
    ) -> AsyncGenerator[StreamEvent, None]:
        """Main async generator streaming real-time execution events for *request*."""
        token = cancellation_token or CancellationToken()
        start_time = datetime.now(UTC)

        try:
            token.raise_if_cancelled()

            # 1. Reasoning Phase
            yield StreamEvent(
                event_type=StreamEventType.REASONING_START,
                content="Analyzing request intent and execution strategy...",
                metadata={"request": request, "task_id": task_id},
            )

            reasoning_res: ReasoningResult = await self.reasoning_engine.reason(
                request, active_file=active_file
            )

            for step in reasoning_res.reasoning_steps:
                token.raise_if_cancelled()
                yield StreamEvent(
                    event_type=StreamEventType.REASONING_STEP,
                    step_number=step.step_number,
                    content=f"[{step.title}] {step.rationale}",
                    metadata={"action": step.action, "status": step.status},
                )

            # Task progress update if task_id supplied
            if task_id and self.task_manager:
                try:
                    await self.task_manager.update_progress(task_id, 25.0, "Reasoning complete")
                    yield StreamEvent(
                        event_type=StreamEventType.TASK_PROGRESS,
                        content=f"Task {task_id} progress updated to 25%",
                        metadata={"task_id": task_id, "progress": 25.0},
                    )
                # Intentionally broad to isolate execution boundaries and prevent crashes.
                except Exception:  # noqa: BLE001
                    logger.warning("TaskManager progress update failed.")

            token.raise_if_cancelled()

            # 2. Planning Phase (if required by strategy)
            if reasoning_res.strategy.requires_planning and reasoning_res.execution_plan:
                yield StreamEvent(
                    event_type=StreamEventType.PLANNING_START,
                    content=f"Formulated goal: {reasoning_res.execution_plan.goal}",
                    metadata={"steps_count": len(reasoning_res.execution_plan.steps)},
                )

                for index, step in enumerate(reasoning_res.execution_plan.steps, start=1):
                    token.raise_if_cancelled()
                    yield StreamEvent(
                        event_type=StreamEventType.PLANNING_STEP,
                        step_number=index,
                        content=f"Step {index}: {step.description}",
                        metadata={"step_id": step.id, "action": step.action},
                    )

            # 3. Tool Execution Stream (if tools selected)
            if reasoning_res.strategy.selected_tools and self.tool_registry:
                for tool_name in reasoning_res.strategy.selected_tools:
                    token.raise_if_cancelled()
                    tool = self.tool_registry.get(tool_name)
                    if not tool:
                        continue

                    # Safety authorization check
                    if self.safety_manager:
                        authorized = await self.safety_manager.authorize(tool_name, request)
                        if not authorized:
                            yield StreamEvent(
                                event_type=StreamEventType.TOOL_FAILED,
                                content=f"Tool '{tool_name}' unauthorized by user safety policy.",
                                metadata={"tool_name": tool_name, "reason": "unauthorized"},
                            )
                            continue

                    yield StreamEvent(
                        event_type=StreamEventType.TOOL_START,
                        content=f"Invoking tool '{tool_name}'...",
                        metadata={"tool_name": tool_name},
                    )

                    yield StreamEvent(
                        event_type=StreamEventType.TOOL_PROGRESS,
                        content=f"Running tool '{tool_name}' execution...",
                        metadata={"tool_name": tool_name, "progress": 50.0},
                    )

                    tool_invocation = ToolInvocation(name=tool_name, arguments={"query": request, "request": request})
                    try:
                        res: ToolResult | None = await self.tool_registry.execute(tool_invocation)
                        content_str = res.content if res else "Tool executed."
                        yield StreamEvent(
                            event_type=StreamEventType.TOOL_COMPLETE,
                            content=content_str,
                            metadata={"tool_name": tool_name, "success": True},
                        )
                    # Intentionally broad to isolate execution boundaries and prevent crashes.
                    except Exception as tool_err:  # noqa: BLE001
                        yield StreamEvent(
                            event_type=StreamEventType.TOOL_FAILED,
                            content=f"Tool '{tool_name}' error: {tool_err}",
                            metadata={"tool_name": tool_name, "success": False},
                        )

            # Task progress update
            if task_id and self.task_manager:
                try:
                    await self.task_manager.update_progress(task_id, 60.0, "Execution phase complete")
                    yield StreamEvent(
                        event_type=StreamEventType.TASK_PROGRESS,
                        content=f"Task {task_id} progress updated to 60%",
                        metadata={"task_id": task_id, "progress": 60.0},
                    )
                # Intentionally broad to isolate execution boundaries and prevent crashes.
                except Exception as err:  # noqa: BLE001
                    logger.warning(f"TaskManager progress update failed: {err}")

            token.raise_if_cancelled()

            # 4. Token-by-Token LLM Response Streaming
            full_response_text = ""
            if self.provider and hasattr(self.provider, "generate_stream") and self.provider.is_configured:
                messages = [{"role": "user", "content": request}]
                async for chunk in self.provider.generate_stream(messages, temperature=0.2):
                    token.raise_if_cancelled()
                    chunk_text = chunk.content if isinstance(chunk, StreamChunk) else str(chunk)
                    full_response_text += chunk_text
                    yield StreamEvent(
                        event_type=StreamEventType.LLM_TOKEN,
                        delta=chunk_text,
                        content=full_response_text,
                    )
            elif reasoning_res.response_text:
                # Simulated streaming chunk delivery for direct response
                for chunk in self._chunk_text(reasoning_res.response_text, chunk_size=15):
                    token.raise_if_cancelled()
                    full_response_text += chunk
                    yield StreamEvent(
                        event_type=StreamEventType.LLM_TOKEN,
                        delta=chunk,
                        content=full_response_text,
                    )
                    await asyncio.sleep(0.01)

            token.raise_if_cancelled()

            # 5. Verification Phase
            if self.verification_engine:
                yield StreamEvent(
                    event_type=StreamEventType.VERIFICATION_START,
                    content="Running project verification suite...",
                )
                try:
                    verif_res = await self.verification_engine.verify()
                    success = getattr(verif_res, "success", True)
                    analysis = getattr(verif_res, "analysis", "Verification completed.")
                    yield StreamEvent(
                        event_type=StreamEventType.VERIFICATION_COMPLETE,
                        content=analysis,
                        metadata={"success": success},
                    )
                # Intentionally broad to isolate execution boundaries and prevent crashes.
                except Exception:  # noqa: BLE001
                    yield StreamEvent(
                        event_type=StreamEventType.VERIFICATION_COMPLETE,
                        content="Verification failed with an internal error.",
                        metadata={"success": False},
                    )

            # Task completion update
            if task_id and self.task_manager:
                try:
                    await self.task_manager.complete_task(task_id, full_response_text or "Stream complete")
                    yield StreamEvent(
                        event_type=StreamEventType.TASK_PROGRESS,
                        content=f"Task {task_id} completed",
                        metadata={"task_id": task_id, "progress": 100.0},
                    )
                # Intentionally broad to isolate execution boundaries and prevent crashes.
                except Exception:  # noqa: BLE001
                    logger.warning("TaskManager completion failed.")

            # Telemetry logging
            duration_ms = (datetime.now(UTC) - start_time).total_seconds() * 1000.0
            self._log_telemetry("stream_completed", duration_ms=duration_ms, task_id=task_id)

            # 6. Completion Event
            yield StreamEvent(
                event_type=StreamEventType.COMPLETION,
                content=full_response_text or "Streaming execution complete.",
                metadata={"duration_ms": round(duration_ms, 2)},
            )

        except asyncio.CancelledError as cancel_err:
            reason_msg = self._scrubber.redact(
                str(cancel_err) or token.reason or "Cancelled"
            )
            self._log_telemetry("stream_cancelled", reason=reason_msg, task_id=task_id)
            if task_id and self.task_manager:
                try:
                    await self.task_manager.cancel_task(task_id, reason=reason_msg)
                # Intentionally broad to isolate execution boundaries and prevent crashes.
                except Exception:  # noqa: BLE001, S110
                    pass
            yield StreamEvent(
                event_type=StreamEventType.CANCELLED,
                content=f"Streaming interrupted: {reason_msg}",
                metadata={"reason": reason_msg},
            )

        # Intentionally broad to isolate execution boundaries and prevent crashes.
        except Exception:  # noqa: BLE001
            logger.error("Streaming execution failed with an internal error.")
            self._log_telemetry("stream_error", error="internal_error", task_id=task_id)
            yield StreamEvent(
                event_type=StreamEventType.ERROR,
                content="Streaming failed with an internal error.",
                metadata={"error": "internal_error"},
            )

    # ---------------------------------------------------------------------------
    # Internal Helpers
    # ---------------------------------------------------------------------------

    @staticmethod
    def _chunk_text(text: str, chunk_size: int = 15) -> list[str]:
        """Split text into uniform character chunks for simulated token streaming."""
        return [text[i : i + chunk_size] for i in range(0, len(text), chunk_size)]

    def _log_telemetry(self, event_type: str, **kwargs: Any) -> None:
        if self.telemetry and hasattr(self.telemetry, "log_event"):
            try:
                self.telemetry.log_event(event_type=f"streaming_{event_type}", **kwargs)
            # Intentionally broad to isolate execution boundaries and prevent crashes.
            except Exception as err:  # noqa: BLE001
                logger.warning(f"Telemetry logging failed: {err}")
