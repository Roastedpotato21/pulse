"""Unit tests for the Pulse Reasoning Engine (pulse.reasoning).

Tests cover:
- Intent analysis and classification (direct answer, tool execution, file edit, search, clarification)
- Execution strategy selection and risk-level integration
- Sanitization of chain-of-thought scratchpads in reasoning steps
- Integration with ContextManager, SafetyManager, ToolRegistry, and RequestPlanner
- Confidence scoring, retries, and heuristic fallback handling
- Handling of empty requests
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

from pulse.core.planner import RequestPlanner
from pulse.reasoning import (
    ExecutionStrategyType,
    IntentAnalysis,
    IntentCategory,
    ReasoningEngine,
    ReasoningResult,
    ReasoningStep,
)
from pulse.safety.safety_manager import RiskLevel, SafetyManager
from pulse.tool_registry import ToolInvocation, ToolRegistry, ToolResult

# ---------------------------------------------------------------------------
# Test Fixtures & Mocks
# ---------------------------------------------------------------------------


class MockTool:
    def __init__(self, name: str, description: str = "Mock Tool") -> None:
        self.name = name
        self.description = description
        self.requires_permission = False

    def matches(self, invocation: ToolInvocation) -> bool:
        return invocation.name == self.name

    async def execute(self, invocation: ToolInvocation) -> ToolResult:
        return ToolResult(content=f"Executed {self.name}")


# ---------------------------------------------------------------------------
# 1. Empty request handling
# ---------------------------------------------------------------------------


def test_reason_empty_request() -> None:
    engine = ReasoningEngine()
    result = asyncio.run(engine.reason(""))

    assert isinstance(result, ReasoningResult)
    assert result.intent.category == IntentCategory.CLARIFICATION_NEEDED
    assert result.strategy.strategy_type == ExecutionStrategyType.CLARIFICATION_REQUEST
    assert "Please provide a message" in (result.response_text or "")
    assert result.overall_confidence == 1.0


# ---------------------------------------------------------------------------
# 2. Intent Analysis - Classification via Heuristics
# ---------------------------------------------------------------------------


def test_analyze_intent_direct_answer() -> None:
    engine = ReasoningEngine()
    intent = asyncio.run(engine.analyze_intent("What is the architecture of the planner?"))

    assert intent.category == IntentCategory.DIRECT_ANSWER
    assert intent.risk_level == RiskLevel.LOW
    assert intent.confidence_score >= 0.7


def test_analyze_intent_repo_search() -> None:
    engine = ReasoningEngine()
    intent = asyncio.run(engine.analyze_intent("Search for DAGPlanner definition in src/pulse/planner/dag_planner.py"))

    assert intent.category == IntentCategory.REPOSITORY_SEARCH
    assert "src/pulse/planner/dag_planner.py" in intent.detected_entities


def test_analyze_intent_file_edit() -> None:
    engine = ReasoningEngine()
    intent = asyncio.run(engine.analyze_intent("Update and fix the bug in config.py"))

    assert intent.category == IntentCategory.FILE_EDIT
    assert intent.risk_level == RiskLevel.MEDIUM


def test_analyze_intent_tool_execution() -> None:
    engine = ReasoningEngine()
    intent = asyncio.run(engine.analyze_intent("Run pytest tests/test_context.py"))

    assert intent.category in (IntentCategory.TOOL_EXECUTION, IntentCategory.FILE_EDIT)


# ---------------------------------------------------------------------------
# 3. Strategy Selection & Safety Manager Risk Integration
# ---------------------------------------------------------------------------


def test_select_strategy_high_risk() -> None:
    safety = SafetyManager()
    engine = ReasoningEngine(safety_manager=safety)

    intent = IntentAnalysis(
        category=IntentCategory.TOOL_EXECUTION,
        summary="Delete production database",
        confidence_score=0.9,
        risk_level=RiskLevel.HIGH,
    )

    strategy = asyncio.run(engine.select_strategy(intent))

    assert strategy.strategy_type == ExecutionStrategyType.SAFE_USER_CONFIRMATION
    assert strategy.estimated_risk == RiskLevel.HIGH
    assert strategy.requires_safety_approval is True
    assert strategy.requires_planning is True


def test_select_strategy_file_edit_medium_risk() -> None:
    engine = ReasoningEngine()

    intent = IntentAnalysis(
        category=IntentCategory.FILE_EDIT,
        summary="Modify file",
        confidence_score=0.85,
        risk_level=RiskLevel.MEDIUM,
    )

    strategy = asyncio.run(engine.select_strategy(intent))

    assert strategy.strategy_type == ExecutionStrategyType.PLANNED_TOOL_EXECUTION
    assert strategy.requires_planning is True
    assert strategy.requires_safety_approval is True


# ---------------------------------------------------------------------------
# 4. Chain-of-Thought Sanitization
# ---------------------------------------------------------------------------


def test_cot_sanitization() -> None:
    engine = ReasoningEngine()
    raw_steps = [
        ReasoningStep(
            step_number=1,
            title="Analysis",
            action="analyze",
            rationale="<think>Internal secret LLM scratchpad reasoning</think>Public summary of intent.",
        ),
        {
            "title": "Execution",
            "action": "execute",
            "rationale": "[scratchpad]Private thinking block[/scratchpad]Public rationale.",
        },
    ]

    sanitized = engine._sanitize_reasoning_steps(raw_steps)

    assert len(sanitized) == 2
    assert "<think>" not in sanitized[0].rationale
    assert "Public summary of intent." in sanitized[0].rationale
    assert "[scratchpad]" not in sanitized[1].rationale
    assert "Public rationale." in sanitized[1].rationale


# ---------------------------------------------------------------------------
# 5. Full Reasoning Workflow with ContextManager & Planner
# ---------------------------------------------------------------------------


def test_full_reasoning_workflow() -> None:
    # ContextManager mock
    ctx_mgr = MagicMock()
    ctx_mgr.as_strings = AsyncMock(return_value=["Context line 1", "Context line 2"])

    # ToolRegistry mock
    registry = ToolRegistry([MockTool("test_runner")])

    engine = ReasoningEngine(
        context_manager=ctx_mgr,
        tool_registry=registry,
        planner=RequestPlanner(),
    )

    result = asyncio.run(engine.reason("How does the planner work?", active_file="src/pulse/context.py"))

    assert isinstance(result, ReasoningResult)
    assert result.intent.category == IntentCategory.DIRECT_ANSWER
    assert result.strategy.strategy_type == ExecutionStrategyType.DIRECT_RESPONSE
    assert len(result.reasoning_steps) >= 3
    assert result.used_fallback is False
    assert result.metadata["active_file"] == "src/pulse/context.py"
    assert result.metadata["context_items_used"] == 2
    ctx_mgr.as_strings.assert_awaited_once_with("How does the planner work?", active_file="src/pulse/context.py")


# ---------------------------------------------------------------------------
# 6. Retry & Fallback Mechanism
# ---------------------------------------------------------------------------


def test_fallback_reasoning_when_provider_fails() -> None:
    failing_provider = MagicMock()
    failing_provider.chat.side_effect = RuntimeError("Provider offline")

    engine = ReasoningEngine(
        provider=failing_provider,
        max_retries=2,
        confidence_threshold=0.6,
    )

    result = asyncio.run(engine.reason("Explain context manager"))

    assert isinstance(result, ReasoningResult)
    assert result.used_fallback is True
    assert result.retries_used == 2
    assert result.intent.category == IntentCategory.DIRECT_ANSWER
