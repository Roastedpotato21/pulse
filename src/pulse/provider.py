from __future__ import annotations

import asyncio
import json
import os
from abc import ABC, abstractmethod
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from pulse.config import ModelConfig, load_env_file
from pulse.core.protocols import LLMProvider, StreamChunk

ModelProvider = LLMProvider


@dataclass(frozen=True)
class ChatMessage:
    role: str
    content: str


class BaseProvider(ABC, LLMProvider):
    """Base class for async-first model providers that are independent from the CLI."""

    def __init__(self, config: ModelConfig, workspace_env_path: Path | str, api_key: str | None = None) -> None:
        env = load_env_file(Path(workspace_env_path)) if not isinstance(workspace_env_path, Path) else load_env_file(workspace_env_path)
        self.config = config
        self.api_key = api_key or os.environ.get(self.api_key_env_var) or env.get(self.api_key_env_var)

    @property
    def is_configured(self) -> bool:
        return bool(self.api_key and self.api_key != "replace_me")

    async def generate_stream(
        self,
        messages: list[dict[str, Any] | ChatMessage],
        temperature: float = 0.2,
    ) -> AsyncGenerator[StreamChunk, None]:
        if not self.is_configured:
            raise RuntimeError(f"{self.api_key_env_var} is not configured.")

        normalized_messages = self._normalize_messages(messages)
        payload = {
            "model": self.config.name,
            "temperature": temperature,
            "max_tokens": self.config.max_tokens,
            "messages": normalized_messages,
            "stream": True,
        }

        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            try:
                async with client.stream("POST", self.endpoint, json=payload, headers=self._headers()) as response:
                    # `httpx` deliberately leaves a streamed response unread.  Read an
                    # error response before raising so its JSON error message is available
                    # to the CLI instead of the generic ``ResponseNotRead`` fallback.
                    if getattr(response, "is_error", False):
                        await response.aread()
                    if hasattr(response, "raise_for_status"):
                        response.raise_for_status()
                    async for line in response.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        payload_line = line[5:].strip()
                        if payload_line == "[DONE]":
                            break
                        if not payload_line:
                            continue
                        yield self._parse_stream_chunk(payload_line)
            except httpx.TimeoutException as error:
                raise RuntimeError("The model request timed out while waiting for a response.") from error
            except httpx.HTTPStatusError as error:
                detail = self._safe_error_detail(error)
                raise RuntimeError(f"Model request failed ({error.response.status_code if error.response else 'unknown'}): {detail}") from error
            except httpx.HTTPError as error:
                raise RuntimeError(f"Model request failed: {error}") from error
            except json.JSONDecodeError as error:
                raise RuntimeError("Received an invalid streaming response from the model provider.") from error

    def chat(self, messages: list[dict[str, Any] | ChatMessage], temperature: float = 0.2) -> str:
        return asyncio.run(self._chat(messages, temperature=temperature))

    def stream_chat(self, messages: list[dict[str, Any] | ChatMessage], temperature: float = 0.2) -> list[str]:
        if not self.is_configured:
            raise RuntimeError(f"{self.api_key_env_var} is not configured.")

        normalized_messages = self._normalize_messages(messages)
        payload = {
            "model": self.config.name,
            "temperature": temperature,
            "max_tokens": self.config.max_tokens,
            "messages": normalized_messages,
            "stream": True,
        }

        try:
            with httpx.stream("POST", self.endpoint, json=payload, headers=self._headers(), timeout=self.timeout_seconds) as response:
                # See the async implementation above: consume error bodies while the
                # stream is open so `_safe_error_detail` can report provider feedback.
                if getattr(response, "is_error", False):
                    response.read()
                if hasattr(response, "raise_for_status"):
                    response.raise_for_status()
                return [self._parse_stream_chunk(line[5:].strip()).content for line in response.iter_lines() if line.startswith("data:") and line[5:].strip() not in {"", "[DONE]"}]
        except httpx.TimeoutException as error:
            raise RuntimeError("The model request timed out while waiting for a response.") from error
        except httpx.HTTPStatusError as error:
            detail = self._safe_error_detail(error)
            raise RuntimeError(f"Model request failed ({error.response.status_code if error.response else 'unknown'}): {detail}") from error
        except httpx.HTTPError as error:
            raise RuntimeError(f"Model request failed: {error}") from error
        except json.JSONDecodeError as error:
            raise RuntimeError("Received an invalid streaming response from the model provider.") from error

    async def _chat(self, messages: list[dict[str, Any] | ChatMessage], *, temperature: float = 0.2) -> str:
        chunks = []
        async for chunk in self.generate_stream(messages, temperature=temperature):
            chunks.append(chunk.content)
        return "".join(chunks)

    async def _stream_chat(self, messages: list[dict[str, Any] | ChatMessage], *, temperature: float = 0.2) -> list[str]:
        return [chunk.content async for chunk in self.generate_stream(messages, temperature=temperature)]

    def _safe_error_detail(self, error: httpx.HTTPError) -> str:
        response = getattr(error, "response", None)
        if response is None:
            return str(error)
        try:
            body = response.text.strip()
        except Exception:
            return str(error)

        if not body:
            return str(error)

        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            return body

        if isinstance(payload, dict):
            provider_error = payload.get("error")
            if isinstance(provider_error, dict):
                message = provider_error.get("message") or provider_error.get("metadata", {}).get("message")
                if isinstance(message, str) and message.strip():
                    return message.strip()
            message = payload.get("message")
            if isinstance(message, str) and message.strip():
                return message.strip()

        return body

    def _normalize_messages(self, messages: list[dict[str, Any] | ChatMessage]) -> list[dict[str, Any]]:
        normalized: list[dict[str, Any]] = []
        for message in messages:
            if isinstance(message, ChatMessage):
                normalized.append({"role": message.role, "content": message.content})
            elif isinstance(message, dict):
                normalized.append(message)
            else:
                raise TypeError("Unsupported message type for provider input.")
        return normalized

    @abstractmethod
    def _headers(self) -> dict[str, str]:
        raise NotImplementedError

    @abstractmethod
    def _parse_stream_chunk(self, payload_line: str) -> StreamChunk:
        raise NotImplementedError

    @property
    @abstractmethod
    def api_key_env_var(self) -> str:
        raise NotImplementedError

    @property
    @abstractmethod
    def endpoint(self) -> str:
        raise NotImplementedError

    @property
    @abstractmethod
    def timeout_seconds(self) -> int:
        raise NotImplementedError


class GeminiProvider(BaseProvider):
    """Gemini-backed provider implementation for Pulse."""

    api_key_env_var = "GEMINI_API_KEY"
    endpoint = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:streamGenerateContent"
    timeout_seconds = 60

    def __init__(self, config: ModelConfig, workspace_env_path: Path | str, api_key: str | None = None) -> None:
        super().__init__(config, workspace_env_path, api_key)

    def _headers(self) -> dict[str, str]:
        return {
            "x-goog-api-key": self.api_key or "",
            "Content-Type": "application/json",
        }

    def _parse_stream_chunk(self, payload_line: str) -> StreamChunk:
        data = json.loads(payload_line)
        candidates = data.get("candidates", [])
        if not candidates:
            return StreamChunk(content="", metadata={"raw": data})

        parts = candidates[0].get("content", {}).get("parts", [])
        text_parts = [part.get("text", "") for part in parts if isinstance(part, dict)]
        content = "".join(text_parts)
        return StreamChunk(content=content, metadata={"raw": data})


class OpenAIProvider(BaseProvider):
    """OpenAI-compatible provider implementation for Pulse."""

    api_key_env_var = "OPENAI_API_KEY"
    endpoint = "https://api.openai.com/v1/chat/completions"
    timeout_seconds = 60

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    def _parse_stream_chunk(self, payload_line: str) -> StreamChunk:
        data = json.loads(payload_line)
        delta = data.get("choices", [{}])[0].get("delta", {})
        content = delta.get("content", "")
        return StreamChunk(content=content, metadata={"raw": data})


class OpenRouterProvider(OpenAIProvider):
    api_key_env_var = "OPENROUTER_API_KEY"
    endpoint = "https://openrouter.ai/api/v1/chat/completions"

    def _headers(self) -> dict[str, str]:
        headers = super()._headers()
        headers.update({
            "HTTP-Referer": "https://local.pulse",
            "X-Title": "Pulse",
        })
        return headers


class ProviderFactory:
    """Create provider implementations without tying them to the CLI."""

    def create(self, provider_name: str, config: ModelConfig, workspace_env_path: Path | str, api_key: str | None = None) -> BaseProvider:
        provider_name = provider_name.lower()
        if provider_name == "gemini":
            return GeminiProvider(config, workspace_env_path, api_key)
        if provider_name in {"openai", "openrouter"}:
            if provider_name == "openrouter":
                return OpenRouterProvider(config, workspace_env_path, api_key)
            return OpenAIProvider(config, workspace_env_path, api_key)
        raise ValueError(f"Unsupported provider: {provider_name}")
