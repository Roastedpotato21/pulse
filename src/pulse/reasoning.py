"""Modular Reasoning Engine for Pulse.

Provides pre-execution intent analysis, risk-aware strategy selection, explicit
sanitized reasoning step generation (without chain-of-thought exposure),
integration with ContextManager, Planner, SafetyManager, and ToolRegistry,
along with retries, confidence scoring, and heuristic fallback logic.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from pulse.core.planner import ExecutionPlan, PlanningRequest, RequestPlanner
from pulse.core.protocols import LLMProvider
from pulse.safety.safety_manager import RiskLevel, SafetyManager
from pulse.tool_registry import ToolRegistry

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Enums and Data Models
# ---------------------------------------------------------------------------


class IntentCategory(Enum):
    """Primary intent categories for user requests."""

    DIRECT_ANSWER = "direct_answer"
    TOOL_EXECUTION = "tool_execution"
    FILE_EDIT = "file_edit"
    REPOSITORY_SEARCH = "repository_search"
    CLARIFICATION_NEEDED = "clarification_needed"


class ExecutionStrategyType(Enum):
    """Optimal execution strategy selected for fulfilling a request."""

    DIRECT_RESPONSE = "direct_response"
    PLANNED_TOOL_EXECUTION = "planned_tool_execution"
    RECURSIVE_DECOMPOSITION = "recursive_decomposition"
    SAFE_USER_CONFIRMATION = "safe_user_confirmation"
    CLARIFICATION_REQUEST = "clarification_request"


@dataclass(slots=True)
class IntentAnalysis:
    """Structured assessment of user prompt intent."""

    category: IntentCategory
    summary: str
    confidence_score: float
    detected_entities: list[str] = field(default_factory=list)
    risk_level: RiskLevel = RiskLevel.LOW
    ambiguities: list[str] = field(default_factory=list)


@dataclass(slots=True)
class ReasoningStep:
    """One explicit, public reasoning step.

    Internal LLM scratchpads / raw chain-of-thought tokens are stripped.
    Only clean, high-level rationale is stored.
    """

    step_number: int
    title: str
    action: str
    rationale: str
    status: str = "completed"


@dataclass(slots=True)
class ExecutionStrategy:
    """Strategy configuration chosen by the Reasoning Engine."""

    strategy_type: ExecutionStrategyType
    steps: list[ReasoningStep]
    estimated_risk: RiskLevel
    requires_planning: bool
    requires_safety_approval: bool
    selected_tools: list[str] = field(default_factory=list)
    confidence: float = 1.0


@dataclass(slots=True)
class ReasoningResult:
    """Final output object from the Reasoning Engine."""

    intent: IntentAnalysis
    strategy: ExecutionStrategy
    reasoning_steps: list[ReasoningStep]
    response_text: str | None = None
    execution_plan: ExecutionPlan | Any | None = None
    overall_confidence: float = 1.0
    retries_used: int = 0
    used_fallback: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Reasoning Engine
# ---------------------------------------------------------------------------


class ReasoningEngine:
    """Provider-agnostic, async Reasoning Engine for Pulse.

    Coordinates intent classification, risk assessment, context gathering,
    tool discovery, execution planning, and response formulation with full
    retry capability and deterministic heuristic fallbacks.
    """

    def __init__(
        self,
        *,
        provider: LLMProvider | Any | None = None,
        context_manager: Any | None = None,
        planner: Any | None = None,
        safety_manager: SafetyManager | None = None,
        tool_registry: ToolRegistry | None = None,
        max_retries: int = 3,
        confidence_threshold: float = 0.6,
    ) -> None:
        self.provider = provider
        self.context_manager = context_manager
        self.planner = planner or RequestPlanner()
        self.safety_manager = safety_manager or SafetyManager()
        self.tool_registry = tool_registry
        self.max_retries = max_retries
        self.confidence_threshold = confidence_threshold

    async def reason(
        self,
        request: str,
        *,
        active_file: str | None = None,
        history: Sequence[Any] | None = None,
    ) -> ReasoningResult:
        """Main async entrypoint for analyzing and reasoning over a request."""
        clean_request = request.strip()
        if not clean_request:
            return self._empty_request_result()

        # 1. Gather context from ContextManager if available
        context_strings: list[str] = []
        if self.context_manager and hasattr(self.context_manager, "as_strings"):
            try:
                context_strings = await self.context_manager.as_strings(
                    clean_request, active_file=active_file
                )
            # Intentionally broad to isolate execution boundaries and prevent crashes.
            except Exception as err:  # noqa: BLE001
                logger.warning(f"ContextManager gather failed: {err}")

        # 2. Analyze intent with retry & fallback
        intent, retries_used, used_fallback = await self._analyze_intent_with_fallback(
            clean_request, context_strings
        )

        # 3. Select optimal execution strategy
        strategy = await self.select_strategy(intent, context_strings)

        # 4. Formulate explicit sanitized reasoning steps
        reasoning_steps = self._sanitize_reasoning_steps(strategy.steps)

        # 5. Build ExecutionPlan if strategy requires planning
        execution_plan: ExecutionPlan | Any | None = None
        if strategy.requires_planning and self.planner:
            try:
                planning_req = PlanningRequest(
                    message=clean_request,
                    metadata={
                        "category": intent.category.value,
                        "risk": intent.risk_level.value,
                        "tools": strategy.selected_tools,
                    },
                )
                if hasattr(self.planner, "plan"):
                    res = self.planner.plan(planning_req)
                    execution_plan = await res if asyncio.iscoroutine(res) else res
            # Intentionally broad to isolate execution boundaries and prevent crashes.
            except Exception as err:  # noqa: BLE001
                logger.warning(f"Planner failed during reasoning: {err}")

        # 6. Formulate immediate direct response text if strategy is Direct Answer or Clarification
        response_text: str | None = None
        if strategy.strategy_type == ExecutionStrategyType.CLARIFICATION_REQUEST:
            response_text = self._format_clarification_prompt(clean_request, intent)
        elif strategy.strategy_type == ExecutionStrategyType.DIRECT_RESPONSE and self.provider:
            response_text = await self._generate_direct_response(clean_request, context_strings)

        overall_confidence = round(
            min(intent.confidence_score, strategy.confidence), 2
        )

        return ReasoningResult(
            intent=intent,
            strategy=strategy,
            reasoning_steps=reasoning_steps,
            response_text=response_text,
            execution_plan=execution_plan,
            overall_confidence=overall_confidence,
            retries_used=retries_used,
            used_fallback=used_fallback,
            metadata={
                "context_items_used": len(context_strings),
                "active_file": active_file,
                "detected_entities_count": len(intent.detected_entities),
            },
        )

    # ---------------------------------------------------------------------------
    # Intent Analysis & Strategy Selection
    # ---------------------------------------------------------------------------

    async def analyze_intent(
        self, request: str, context: list[str] | None = None
    ) -> IntentAnalysis:
        """Analyzes request to classify intent, risk level, and detected entities."""
        if not request.strip():
            return self._heuristic_intent_analysis("")

        if self.provider and hasattr(self.provider, "chat"):
            try:
                # LLM-based intent extraction
                intent_data = await self._llm_intent_classification(request, context)
                if intent_data and intent_data.confidence_score >= self.confidence_threshold:
                    return intent_data
            # Intentionally broad to isolate execution boundaries and prevent crashes.
            except Exception as err:  # noqa: BLE001
                logger.debug(f"LLM intent analysis exception: {err}")

        # Deterministic fallback
        return self._heuristic_intent_analysis(request)

    async def select_strategy(
        self, intent: IntentAnalysis, context: list[str] | None = None
    ) -> ExecutionStrategy:
        """Determines the optimal execution strategy based on intent, safety, and tools."""
        # Risk assessment via SafetyManager
        risk = intent.risk_level
        selected_tools: list[str] = []

        # Tool discovery via ToolRegistry
        if self.tool_registry:
            registered_tools = self.tool_registry.discover()
            for tool in registered_tools:
                # Simple keyword/matching heuristic for tool selection
                if any(
                    entity.lower() in tool.name.lower() or tool.name.lower() in entity.lower()
                    for entity in intent.detected_entities
                ) or (intent.category == IntentCategory.TOOL_EXECUTION and tool.name):
                    selected_tools.append(tool.name)

        # Strategy decision matrix
        if intent.category == IntentCategory.CLARIFICATION_NEEDED:
            strategy_type = ExecutionStrategyType.CLARIFICATION_REQUEST
            requires_planning = False
            requires_safety = False
        elif risk == RiskLevel.HIGH:
            strategy_type = ExecutionStrategyType.SAFE_USER_CONFIRMATION
            requires_planning = True
            requires_safety = True
        elif intent.category == IntentCategory.FILE_EDIT or intent.category == IntentCategory.TOOL_EXECUTION:
            strategy_type = ExecutionStrategyType.PLANNED_TOOL_EXECUTION
            requires_planning = True
            requires_safety = risk in (RiskLevel.MEDIUM, RiskLevel.HIGH)
        elif intent.category == IntentCategory.REPOSITORY_SEARCH:
            strategy_type = ExecutionStrategyType.PLANNED_TOOL_EXECUTION
            requires_planning = False
            requires_safety = False
        else:  # DIRECT_ANSWER
            strategy_type = ExecutionStrategyType.DIRECT_RESPONSE
            requires_planning = False
            requires_safety = False

        steps = self._build_strategy_steps(intent, strategy_type, risk, selected_tools)

        return ExecutionStrategy(
            strategy_type=strategy_type,
            steps=steps,
            estimated_risk=risk,
            requires_planning=requires_planning,
            requires_safety_approval=requires_safety,
            selected_tools=selected_tools,
            confidence=intent.confidence_score,
        )

    # ---------------------------------------------------------------------------
    # Internal Helpers, Fallbacks & Sanitization
    # ---------------------------------------------------------------------------

    async def _analyze_intent_with_fallback(
        self, request: str, context: list[str]
    ) -> tuple[IntentAnalysis, int, bool]:
        """Runs intent analysis with retries and deterministic fallback."""
        if not self.provider:
            return self._heuristic_intent_analysis(request), 0, False

        retries = 0
        for attempt in range(self.max_retries):
            try:
                intent = await self._llm_intent_classification(request, context)
                if intent and intent.confidence_score >= self.confidence_threshold:
                    return intent, attempt, False
            # Intentionally broad to isolate execution boundaries and prevent crashes.
            except Exception as err:  # noqa: BLE001
                logger.warning(f"LLM intent classification attempt {attempt + 1} failed: {err}")
            retries += 1

        fallback_intent = self._heuristic_intent_analysis(request)
        return fallback_intent, retries, True

    def _heuristic_intent_analysis(self, request: str) -> IntentAnalysis:
        """Deterministic heuristic analysis for rule-based fallback and instant parsing."""
        req_lower = request.lower().strip()
        if not req_lower:
            return IntentAnalysis(
                category=IntentCategory.CLARIFICATION_NEEDED,
                summary="Empty user prompt provided.",
                confidence_score=1.0,
                risk_level=RiskLevel.LOW,
                ambiguities=["No request content."],
            )

        # Detect files & symbols
        entities = re.findall(r"[a-zA-Z0-9_./-]+\.[a-zA-Z0-9_]+", request)

        # Risk assessment
        risk = self.safety_manager.assess_risk(request)

        # Heuristic rules
        if any(kw in req_lower for kw in ["how much work", "what is", "explain", "describe", "why does", "tell me about"]):
            category = IntentCategory.DIRECT_ANSWER
            confidence = 0.85
        elif any(kw in req_lower for kw in ["find", "search", "where is", "grep", "locate"]):
            category = IntentCategory.REPOSITORY_SEARCH
            confidence = 0.85
        elif any(kw in req_lower for kw in ["fix", "edit", "update", "modify", "add", "change", "refactor", "implement", "write"]):
            category = IntentCategory.FILE_EDIT
            confidence = 0.80
        elif any(kw in req_lower for kw in ["run", "execute", "test", "pytest", "build", "terminal", "command"]):
            category = IntentCategory.TOOL_EXECUTION
            confidence = 0.80
        elif len(req_lower.split()) < 2:
            category = IntentCategory.CLARIFICATION_NEEDED
            confidence = 0.90
        else:
            category = IntentCategory.DIRECT_ANSWER
            confidence = 0.70

        return IntentAnalysis(
            category=category,
            summary=f"Parsed intent: {category.value}",
            confidence_score=confidence,
            detected_entities=list(dict.fromkeys(entities)),
            risk_level=risk,
            ambiguities=[],
        )

    async def _llm_intent_classification(
        self, request: str, context: list[str] | None
    ) -> IntentAnalysis | None:
        """Queries LLM provider for structured intent classification."""
        if not self.provider:
            return None

        prompt = (
            "Analyze the following user prompt for a software engineering assistant.\n"
            "Output JSON with fields:\n"
            '  "category": one of ["direct_answer", "tool_execution", "file_edit", "repository_search", "clarification_needed"]\n'
            '  "summary": concise string summary of user intent\n'
            '  "confidence": float between 0.0 and 1.0\n'
            '  "entities": array of file paths or code symbols referenced\n'
            '  "ambiguities": array of missing or ambiguous parameters\n\n'
            f"Prompt: {request}\n"
        )

        messages = [{"role": "user", "content": prompt}]
        if hasattr(self.provider, "_chat") and asyncio.iscoroutinefunction(self.provider._chat):
            raw_response = await self.provider._chat(messages, temperature=0.1)
        else:
            res = await asyncio.to_thread(self.provider.chat, messages, 0.1)
            raw_response = str(await res) if (asyncio.iscoroutine(res) or hasattr(res, "__await__")) else str(res)

        # Parse JSON from LLM response
        match = re.search(r"\{.*\}", raw_response, re.DOTALL)
        if not match:
            return None

        data = json.loads(match.group(0))
        cat_str = data.get("category", "direct_answer")
        category = next((c for c in IntentCategory if c.value == cat_str), IntentCategory.DIRECT_ANSWER)
        risk = self.safety_manager.assess_risk(request)

        return IntentAnalysis(
            category=category,
            summary=data.get("summary", "LLM Intent Analysis"),
            confidence_score=float(data.get("confidence", 0.8)),
            detected_entities=data.get("entities", []),
            risk_level=risk,
            ambiguities=data.get("ambiguities", []),
        )

    def _sanitize_reasoning_steps(
        self, steps: Sequence[ReasoningStep | dict[str, Any]]
    ) -> list[ReasoningStep]:
        """Ensures reasoning steps are clean public summaries without raw CoT / scratchpads."""
        sanitized: list[ReasoningStep] = []
        for index, item in enumerate(steps, start=1):
            if isinstance(item, ReasoningStep):
                clean_rationale = self._strip_cot(item.rationale)
                sanitized.append(
                    ReasoningStep(
                        step_number=index,
                        title=item.title,
                        action=item.action,
                        rationale=clean_rationale,
                        status=item.status,
                    )
                )
            elif isinstance(item, dict):
                clean_rationale = self._strip_cot(str(item.get("rationale", "")))
                sanitized.append(
                    ReasoningStep(
                        step_number=index,
                        title=str(item.get("title", f"Step {index}")),
                        action=str(item.get("action", "evaluate")),
                        rationale=clean_rationale,
                        status=str(item.get("status", "completed")),
                    )
                )
        return sanitized

    @staticmethod
    def _strip_cot(text: str) -> str:
        """Removes internal chain-of-thought blocks (<think>, [scratchpad], etc.)."""
        cleaned = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
        cleaned = re.sub(r"\[scratchpad\].*?\[/scratchpad\]", "", cleaned, flags=re.DOTALL)
        return cleaned.strip()

    def _build_strategy_steps(
        self,
        intent: IntentAnalysis,
        strategy_type: ExecutionStrategyType,
        risk: RiskLevel,
        tools: list[str],
    ) -> list[ReasoningStep]:
        """Generates standard public reasoning steps based on strategy decision."""
        steps: list[ReasoningStep] = [
            ReasoningStep(
                step_number=1,
                title="Intent Analysis",
                action="analyze_intent",
                rationale=f"Assessed user request as '{intent.category.value}' with {risk.value} risk level.",
            ),
            ReasoningStep(
                step_number=2,
                title="Safety & Context Check",
                action="verify_safety",
                rationale=f"Evaluated execution safety ({risk.value} risk) and gathered active context.",
            ),
        ]

        if strategy_type == ExecutionStrategyType.CLARIFICATION_REQUEST:
            steps.append(
                ReasoningStep(
                    step_number=3,
                    title="Formulate Clarification Request",
                    action="request_clarification",
                    rationale="Identified ambiguity or underspecified requirements in prompt.",
                )
            )
        elif strategy_type == ExecutionStrategyType.SAFE_USER_CONFIRMATION:
            steps.append(
                ReasoningStep(
                    step_number=3,
                    title="Prepare User Confirmation",
                    action="request_confirmation",
                    rationale=f"High-risk action detected ({risk.value}). Requires explicit user approval.",
                )
            )
        elif strategy_type == ExecutionStrategyType.PLANNED_TOOL_EXECUTION:
            steps.append(
                ReasoningStep(
                    step_number=3,
                    title="Formulate Execution Plan",
                    action="generate_plan",
                    rationale=f"Constructed structured execution plan utilizing tools: {tools or ['default_tools']}.",
                )
            )
        else:  # DIRECT_RESPONSE
            steps.append(
                ReasoningStep(
                    step_number=3,
                    title="Generate Direct Response",
                    action="direct_answer",
                    rationale="Formulated direct textual answer from gathered context.",
                )
            )

        return steps

    def _format_clarification_prompt(self, request: str, intent: IntentAnalysis) -> str:
        """Builds user-facing clarification message."""
        ambiguities = ", ".join(intent.ambiguities) if intent.ambiguities else "underspecified parameters"
        return (
            f"Could you please clarify your request ('{request}')? "
            f"Additional details regarding {ambiguities} will help me provide an accurate solution."
        )

    async def _generate_direct_response(
        self, request: str, context: list[str]
    ) -> str:
        """Generates direct response via LLM provider."""
        if not self.provider:
            return f"Processed request: {request}"

        prompt = "Answer the request based on the context:\n\nContext:\n" + "\n".join(context) + f"\n\nRequest: {request}"
        messages = [{"role": "user", "content": prompt}]
        try:
            if hasattr(self.provider, "_chat") and asyncio.iscoroutinefunction(self.provider._chat):
                return await self.provider._chat(messages, temperature=0.2)
            res = await asyncio.to_thread(self.provider.chat, messages, 0.2)
            if asyncio.iscoroutine(res) or hasattr(res, "__await__"):
                return str(await res)
            return str(res)
        # Intentionally broad to isolate execution boundaries and prevent crashes.
        except Exception as err:  # noqa: BLE001
            logger.warning(f"Direct response generation failed: {err}")
            return f"Direct response unavailable due to provider error ({err})."

    def _empty_request_result(self) -> ReasoningResult:
        """Handles empty requests gracefully."""
        intent = IntentAnalysis(
            category=IntentCategory.CLARIFICATION_NEEDED,
            summary="Empty user prompt.",
            confidence_score=1.0,
            risk_level=RiskLevel.LOW,
            ambiguities=["No message provided."],
        )
        strategy = ExecutionStrategy(
            strategy_type=ExecutionStrategyType.CLARIFICATION_REQUEST,
            steps=[
                ReasoningStep(
                    step_number=1,
                    title="Empty Request",
                    action="clarify",
                    rationale="User submitted empty text prompt.",
                )
            ],
            estimated_risk=RiskLevel.LOW,
            requires_planning=False,
            requires_safety_approval=False,
        )
        return ReasoningResult(
            intent=intent,
            strategy=strategy,
            reasoning_steps=strategy.steps,
            response_text="Please provide a message or question to process.",
            overall_confidence=1.0,
        )
