from __future__ import annotations

import json
from typing import Any

from pulse.core.protocols import StreamChunk
from pulse.providers.base import BaseProvider, ChatMessage


class GeminiProvider(BaseProvider):
    """Google Gemini provider implementation for Pulse."""

    api_key_env_var = "GEMINI_API_KEY"

    @property
    def endpoint(self) -> str:
        model_name = self.config.name
        if not model_name.startswith("models/"):
            model_name = f"models/{model_name}"
        return f"https://generativelanguage.googleapis.com/v1beta/{model_name}:streamGenerateContent"

    def _headers(self) -> dict[str, str]:
        return {
            "x-goog-api-key": self.api_key or "",
            "Content-Type": "application/json",
        }

    def _build_payload(
        self,
        messages: list[dict[str, Any] | ChatMessage],
        temperature: float = 0.2,
    ) -> dict[str, Any]:
        normalized = self._normalize_messages(messages)
        contents: list[dict[str, Any]] = []
        system_instruction = None

        for msg in normalized:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            if role == "system":
                system_instruction = {"parts": [{"text": str(content)}]}
            else:
                gemini_role = "model" if role == "assistant" else "user"
                contents.append({"role": gemini_role, "parts": [{"text": str(content)}]})

        payload: dict[str, Any] = {
            "contents": contents,
            "generationConfig": {
                "temperature": temperature,
                "maxOutputTokens": self.config.max_tokens,
            },
        }
        if system_instruction:
            payload["systemInstruction"] = system_instruction
        return payload

    def _parse_stream_chunk(self, payload_line: str) -> StreamChunk:
        data = json.loads(payload_line)
        candidates = data.get("candidates", [])
        if not candidates:
            return StreamChunk(content="", metadata={"raw": data})

        parts = candidates[0].get("content", {}).get("parts", [])
        text_parts = [part.get("text", "") for part in parts if isinstance(part, dict)]
        content = "".join(text_parts)
        return StreamChunk(content=content, metadata={"raw": data})
