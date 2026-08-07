from __future__ import annotations

from pulse.providers.openai import OpenAIProvider


class DeepSeekProvider(OpenAIProvider):
    """DeepSeek provider implementation for Pulse."""

    api_key_env_var = "DEEPSEEK_API_KEY"
    endpoint = "https://api.deepseek.com/chat/completions"
