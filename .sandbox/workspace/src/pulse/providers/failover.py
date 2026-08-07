from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any

from pulse.core.protocols import LLMProvider, StreamChunk


class FailoverProvider(LLMProvider):
    """Fallback mechanism to switch to a secondary configured provider if the primary API call fails or times out."""

    def __init__(self, primary: LLMProvider, secondary: LLMProvider) -> None:
        self.primary = primary
        self.secondary = secondary

    @property
    def is_configured(self) -> bool:
        return getattr(self.primary, "is_configured", True) or getattr(self.secondary, "is_configured", True)

    async def generate_stream(
        self,
        messages: list[dict[str, Any]],
        temperature: float = 0.2,
    ) -> AsyncGenerator[StreamChunk, None]:
        try:
            async for chunk in self.primary.generate_stream(messages, temperature=temperature):
                yield chunk
        # Intentionally broad to isolate execution boundaries and prevent crashes.
        except Exception:  # noqa: BLE001
            # Fallback to secondary provider if primary fails or times out
            async for chunk in self.secondary.generate_stream(messages, temperature=temperature):
                yield chunk
