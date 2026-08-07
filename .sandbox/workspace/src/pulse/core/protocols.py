from __future__ import annotations

from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from typing import Any, Protocol

from pulse.config import ModelConfig


@dataclass(slots=True)
class StreamChunk:
    """A single streamed token or chunk emitted by an LLM provider."""

    content: str
    metadata: dict[str, Any] = field(default_factory=dict)


class LLMProvider(Protocol):
    """Unified structural contract for any AI engine plugged into Pulse."""

    config: ModelConfig

    @property
    def is_configured(self) -> bool:
        """Whether the provider has enough credentials to call the model."""
        ...

    async def generate_stream(
        self,
        messages: list[dict[str, Any]],
        temperature: float = 0.2,
    ) -> AsyncGenerator[StreamChunk, None]:
        """Yield streamed response chunks for the provided conversation messages."""

    def chat(self, messages: list[Any], temperature: float = 0.2) -> str:
        """Return a complete response for the provided conversation messages."""
        ...
