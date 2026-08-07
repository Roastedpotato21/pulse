from __future__ import annotations

from pulse.providers.openai import OpenAIProvider


class GroqProvider(OpenAIProvider):
    """Groq provider implementation for Pulse."""

    api_key_env_var = "GROQ_API_KEY"
    endpoint = "https://api.groq.com/openai/v1/chat/completions"
