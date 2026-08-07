from __future__ import annotations

from pulse.providers.openai import OpenAIProvider


class OpenRouterProvider(OpenAIProvider):
    """OpenRouter provider implementation for Pulse."""

    api_key_env_var = "OPENROUTER_API_KEY"
    endpoint = "https://openrouter.ai/api/v1/chat/completions"

    def _headers(self) -> dict[str, str]:
        headers = super()._headers()
        headers.update(
            {
                "HTTP-Referer": "https://local.pulse",
                "X-Title": "Pulse",
            }
        )
        return headers
