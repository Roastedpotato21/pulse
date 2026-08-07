from __future__ import annotations

import json

from pulse.core.protocols import StreamChunk
from pulse.providers.base import BaseProvider


class OpenAIProvider(BaseProvider):
    """OpenAI provider implementation for Pulse."""

    api_key_env_var = "OPENAI_API_KEY"
    endpoint = "https://api.openai.com/v1/chat/completions"

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key or ''}",
            "Content-Type": "application/json",
        }

    def _parse_stream_chunk(self, payload_line: str) -> StreamChunk:
        data = json.loads(payload_line)
        choices = data.get("choices", [])
        if not choices:
            return StreamChunk(content="", metadata={"raw": data})

        delta = choices[0].get("delta", {})
        content = delta.get("content", "")
        return StreamChunk(content=content or "", metadata={"raw": data})
