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
    """Base class for async-first model providers independent from the CLI."""

    def __init__(
        self,
        config: ModelConfig,
        workspace_env_path: Path | str,
        api_key: str | None = None,
    ) -> None:
        env_path = (
            Path(workspace_env_path)
            if isinstance(workspace_env_path, str)
            else workspace_env_path
        )
        env = load_env_file(env_path) if env_path.exists() else {}
        self.config = config
        from pulse.provider_keys import ProviderKeyStore

        self.api_key = (
            api_key
            or ProviderKeyStore(env_path.parent).get(config.provider)
            or os.environ.get(self.api_key_env_var)
            or env.get(self.api_key_env_var)
        )

    @property
    def is_configured(self) -> bool:
        return bool(self.api_key and self.api_key.strip() and self.api_key != "replace_me")

    async def generate_stream(
        self,
        messages: list[dict[str, Any] | ChatMessage],
        temperature: float = 0.2,
    ) -> AsyncGenerator[StreamChunk, None]:
        if not self.is_configured:
            raise RuntimeError(
                f"{self.api_key_env_var} is not configured. Run 'pulse keys' to add it securely."
            )

        payload = self._build_payload(messages, temperature=temperature)
        endpoint = self.endpoint

        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            try:
                async with client.stream(
                    "POST", endpoint, json=payload, headers=self._headers()
                ) as response:
                    if getattr(response, "is_error", False):
                        await response.aread()
                    if hasattr(response, "raise_for_status"):
                        response.raise_for_status()

                    async for line in response.aiter_lines():
                        chunk = self._process_stream_line(line)
                        if chunk is not None:
                            yield chunk
            except httpx.TimeoutException as error:
                raise RuntimeError(
                    f"The model request to {self.config.provider} timed out."
                ) from error
            except httpx.HTTPStatusError as error:
                detail = self._safe_error_detail(error)
                code = error.response.status_code if error.response else "unknown"
                raise RuntimeError(
                    f"Model request failed ({self.config.provider} HTTP {code}): {detail}"
                ) from error
            except httpx.HTTPError as error:
                raise RuntimeError(
                    f"Model request to {self.config.provider} failed due to a network error."
                ) from error
            except json.JSONDecodeError as error:
                raise RuntimeError(
                    f"Received an invalid streaming response from {self.config.provider}."
                ) from error

    def chat(
        self,
        messages: list[dict[str, Any] | ChatMessage],
        temperature: float = 0.2,
    ) -> str:
        return asyncio.run(self._chat(messages, temperature=temperature))

    def stream_chat(
        self,
        messages: list[dict[str, Any] | ChatMessage],
        temperature: float = 0.2,
    ) -> list[str]:
        if not self.is_configured:
            raise RuntimeError(
                f"{self.api_key_env_var} is not configured. Run 'pulse keys' to add it securely."
            )

        payload = self._build_payload(messages, temperature=temperature)
        try:
            with httpx.stream(
                "POST",
                self.endpoint,
                json=payload,
                headers=self._headers(),
                timeout=self.timeout_seconds,
            ) as response:
                if getattr(response, "is_error", False):
                    response.read()
                if hasattr(response, "raise_for_status"):
                    response.raise_for_status()

                results: list[str] = []
                for line in response.iter_lines():
                    chunk = self._process_stream_line(line)
                    if chunk is not None and chunk.content:
                        results.append(chunk.content)
                return results
        except httpx.TimeoutException as error:
            raise RuntimeError(
                f"The model request to {self.config.provider} timed out."
            ) from error
        except httpx.HTTPStatusError as error:
            detail = self._safe_error_detail(error)
            code = error.response.status_code if error.response else "unknown"
            raise RuntimeError(
                f"Model request failed ({self.config.provider} HTTP {code}): {detail}"
            ) from error
        except httpx.HTTPError as error:
            raise RuntimeError(
                f"Model request to {self.config.provider} failed due to a network error."
            ) from error
        except json.JSONDecodeError as error:
            raise RuntimeError(
                f"Received an invalid streaming response from {self.config.provider}."
            ) from error

    async def _chat(
        self,
        messages: list[dict[str, Any] | ChatMessage],
        *,
        temperature: float = 0.2,
    ) -> str:
        chunks = []
        async for chunk in self.generate_stream(messages, temperature=temperature):
            chunks.append(chunk.content)
        return "".join(chunks)

    async def _stream_chat(
        self,
        messages: list[dict[str, Any] | ChatMessage],
        *,
        temperature: float = 0.2,
    ) -> list[str]:
        return [
            chunk.content
            async for chunk in self.generate_stream(messages, temperature=temperature)
        ]

    def _build_payload(
        self,
        messages: list[dict[str, Any] | ChatMessage],
        temperature: float = 0.2,
    ) -> dict[str, Any]:
        normalized = self._normalize_messages(messages)
        return {
            "model": self.config.name,
            "temperature": temperature,
            "max_tokens": self.config.max_tokens,
            "messages": normalized,
            "stream": True,
        }

    def _process_stream_line(self, line: str) -> StreamChunk | None:
        if not line.startswith("data:"):
            return None
        payload_line = line[5:].strip()
        if not payload_line or payload_line == "[DONE]":
            return None
        return self._parse_stream_chunk(payload_line)

    def _safe_error_detail(self, error: httpx.HTTPError) -> str:
        response = getattr(error, "response", None)
        if response is None:
            return "The provider request failed."
        status = getattr(response, "status_code", 0)

        if status == 401:
            return f"Invalid or unauthenticated API key ({self.api_key_env_var})."
        if status == 404:
            return f"Model '{self.config.name}' was not found or is unavailable for {self.config.provider}."
        if status == 429:
            return f"Rate limit exceeded for {self.config.provider}. Please wait before retrying."

        # Provider-controlled bodies may reflect prompts, request payloads, or
        # credentials. They must never cross into CLI, RPC, telemetry, or logs.
        return "The provider rejected the request. Check provider status and configuration."

    def _normalize_messages(
        self, messages: list[dict[str, Any] | ChatMessage]
    ) -> list[dict[str, Any]]:
        normalized: list[dict[str, Any]] = []
        for msg in messages:
            if isinstance(msg, ChatMessage):
                normalized.append({"role": msg.role, "content": msg.content})
            elif isinstance(msg, dict):
                normalized.append(msg)
            else:
                raise TypeError(f"Unsupported message type: {type(msg)}")
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
    def timeout_seconds(self) -> int:
        return 60
