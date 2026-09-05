from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from dataclasses import replace
from pathlib import Path
from typing import Any

from pulse.config import ModelConfig
from pulse.core.protocols import StreamChunk
from pulse.providers.base import BaseProvider, ChatMessage, ProviderRequestError
from pulse.providers.discovery import ModelDiscoveryError
from pulse.providers.manager import ProviderManager


class AutoModelProvider:
    """Resolve a replacement model once when an auto-selected model disappears."""

    def __init__(
        self,
        manager: ProviderManager,
        provider: BaseProvider,
        workspace_env_path: Path,
    ) -> None:
        self._manager = manager
        self._provider = provider
        self._workspace_env_path = workspace_env_path

    @property
    def config(self) -> ModelConfig:
        return self._provider.config

    @property
    def api_key(self) -> str | None:
        return self._provider.api_key

    @property
    def api_key_env_var(self) -> str:
        return self._provider.api_key_env_var

    @property
    def is_configured(self) -> bool:
        return self._provider.is_configured

    async def generate_stream(
        self,
        messages: list[dict[str, Any] | ChatMessage],
        temperature: float = 0.2,
    ) -> AsyncGenerator[StreamChunk, None]:
        emitted = False
        try:
            async for chunk in self._provider.generate_stream(messages, temperature):
                emitted = True
                yield chunk
            return
        except ProviderRequestError as error:
            if emitted or error.status_code not in {404, 410} or not self.api_key:
                raise
            original = error

        try:
            replacement = await asyncio.to_thread(
                self._manager.resolve_auto_model,
                self.config.provider,
                self.api_key,
                excluded={self.config.name},
            )
        except ModelDiscoveryError:
            raise original

        new_config = replace(self.config, name=replacement)
        self._provider = self._manager.create_provider(
            new_config,
            self._workspace_env_path,
            api_key=self.api_key,
        )
        async for chunk in self._provider.generate_stream(messages, temperature):
            yield chunk

    def chat(
        self,
        messages: list[dict[str, Any] | ChatMessage],
        temperature: float = 0.2,
    ) -> str:
        return asyncio.run(self._chat(messages, temperature))

    async def _chat(
        self,
        messages: list[dict[str, Any] | ChatMessage],
        temperature: float,
    ) -> str:
        chunks = [
            chunk.content
            async for chunk in self.generate_stream(messages, temperature)
        ]
        return "".join(chunks)
