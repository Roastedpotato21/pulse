from __future__ import annotations

from pathlib import Path

import httpx

from pulse.config import ModelConfig
from pulse.core.protocols import LLMProvider
from pulse.providers.anthropic import AnthropicProvider
from pulse.providers.base import BaseProvider, ChatMessage
from pulse.providers.deepseek import DeepSeekProvider
from pulse.providers.gemini import GeminiProvider
from pulse.providers.groq import GroqProvider
from pulse.providers.openai import OpenAIProvider
from pulse.providers.openrouter import OpenRouterProvider

ModelProvider = LLMProvider


class ProviderFactory:
    """Create provider implementations without tying them to the CLI."""

    def create(
        self,
        provider_name: str,
        config: ModelConfig,
        workspace_env_path: Path | str,
        api_key: str | None = None,
    ) -> BaseProvider:
        p = provider_name.lower().strip()
        if p == "gemini":
            return GeminiProvider(config, workspace_env_path, api_key)
        if p == "openrouter":
            return OpenRouterProvider(config, workspace_env_path, api_key)
        if p == "openai":
            return OpenAIProvider(config, workspace_env_path, api_key)
        if p == "anthropic":
            return AnthropicProvider(config, workspace_env_path, api_key)
        if p == "groq":
            return GroqProvider(config, workspace_env_path, api_key)
        if p == "deepseek":
            return DeepSeekProvider(config, workspace_env_path, api_key)

        raise ValueError(f"Unsupported provider: {provider_name}")


__all__ = [
    "AnthropicProvider",
    "BaseProvider",
    "ChatMessage",
    "DeepSeekProvider",
    "GeminiProvider",
    "GroqProvider",
    "ModelProvider",
    "OpenAIProvider",
    "OpenRouterProvider",
    "ProviderFactory",
    "httpx",
]
