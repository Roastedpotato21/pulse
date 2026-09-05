from __future__ import annotations

import json
from typing import Any

from pulse.core.protocols import StreamChunk
from pulse.providers.base import BaseProvider, ChatMessage


class AnthropicProvider(BaseProvider):
    """Anthropic Claude Messages API provider for Pulse."""

    api_key_env_var = "ANTHROPIC_API_KEY"
    endpoint = "https://api.anthropic.com/v1/messages"

    def _headers(self) -> dict[str, str]:
        return {
            "x-api-key": self.api_key or "",
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }

    def _build_payload(
        self,
        messages: list[dict[str, Any] | ChatMessage],
        temperature: float = 0.2,
    ) -> dict[str, Any]:
        normalized = self._normalize_messages(messages)
        system_prompts: list[str] = []
        anthropic_messages: list[dict[str, Any]] = []

        for msg in normalized:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            if role == "system":
                system_prompts.append(str(content))
            else:
                anthropic_role = "assistant" if role == "assistant" else "user"
                anthropic_messages.append({"role": anthropic_role, "content": str(content)})

        payload: dict[str, Any] = {
            "model": self.config.name,
            "max_tokens": self.config.max_tokens,
            "messages": anthropic_messages,
            "stream": True,
        }
        if system_prompts:
            payload["system"] = "\n\n".join(system_prompts)
        rejects_temperature = self.config.name.lower().startswith(
            ("claude-opus-4-7", "claude-opus-4-8", "claude-opus-5", "claude-mythos")
        )
        if temperature is not None and not rejects_temperature:
            payload["temperature"] = temperature
        return payload

    def _parse_stream_chunk(self, payload_line: str) -> StreamChunk:
        try:
            data = json.loads(payload_line)
        except json.JSONDecodeError:
            return StreamChunk(content="", metadata={"raw_line": payload_line})

        event_type = data.get("type")
        if event_type == "content_block_delta":
            delta = data.get("delta", {})
            if delta.get("type") == "text_delta":
                return StreamChunk(content=delta.get("text", ""), metadata={"raw": data})

        return StreamChunk(content="", metadata={"raw": data})
