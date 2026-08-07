from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar


class BudgetExceededError(Exception):
    """Raised when token or cost budget limit is exceeded."""


@dataclass
class ModelPricing:
    prompt_price_per_1k: float
    completion_price_per_1k: float


@dataclass
class UsageRecord:
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    estimated_cost: float


class CostTracker:
    """Tracks prompt/completion token usage across models (OpenAI, Gemini, OpenRouter).

    Enforces maximum daily/session token and cost budgets.
    """

    DEFAULT_PRICING: ClassVar[dict[str, ModelPricing]] = {
        "gpt-4o": ModelPricing(0.0025, 0.0100),
        "gpt-4o-mini": ModelPricing(0.00015, 0.0006),
        "gemini-2.0-flash": ModelPricing(0.0001, 0.0004),
        "openrouter/auto": ModelPricing(0.0010, 0.0030),
        "default": ModelPricing(0.0015, 0.0050),
    }

    def __init__(
        self,
        max_session_tokens: int | None = None,
        max_session_cost: float | None = None,
        pricing: dict[str, ModelPricing] | None = None,
    ) -> None:
        self.max_session_tokens = max_session_tokens
        self.max_session_cost = max_session_cost
        self.pricing = pricing or self.DEFAULT_PRICING

        self._total_prompt_tokens: int = 0
        self._total_completion_tokens: int = 0
        self._total_cost: float = 0.0

    @property
    def total_tokens(self) -> int:
        return self._total_prompt_tokens + self._total_completion_tokens

    @property
    def total_cost(self) -> float:
        return round(self._total_cost, 6)

    def record_usage(self, model: str, prompt_tokens: int, completion_tokens: int) -> UsageRecord:
        added_tokens = prompt_tokens + completion_tokens

        if self.max_session_tokens is not None and (self.total_tokens + added_tokens) > self.max_session_tokens:
            raise BudgetExceededError(
                f"Session token budget exceeded: {self.total_tokens + added_tokens} > {self.max_session_tokens}"
            )

        model_key = model.lower()
        pricing = self.pricing.get(model_key) or self.pricing.get("default", ModelPricing(0.0015, 0.0050))

        prompt_cost = (prompt_tokens / 1000.0) * pricing.prompt_price_per_1k
        completion_cost = (completion_tokens / 1000.0) * pricing.completion_price_per_1k
        cost = prompt_cost + completion_cost

        if self.max_session_cost is not None and (self._total_cost + cost) > self.max_session_cost:
            raise BudgetExceededError(
                f"Session cost budget exceeded: ${self._total_cost + cost:.4f} > ${self.max_session_cost:.4f}"
            )

        self._total_prompt_tokens += prompt_tokens
        self._total_completion_tokens += completion_tokens
        self._total_cost += cost

        return UsageRecord(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=added_tokens,
            estimated_cost=round(cost, 6),
        )

    def reset(self) -> None:
        self._total_prompt_tokens = 0
        self._total_completion_tokens = 0
        self._total_cost = 0.0
