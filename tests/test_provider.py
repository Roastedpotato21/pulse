from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx

from pulse.config import ModelConfig
from pulse.provider import ChatMessage, OpenRouterProvider


def test_stream_chat_yields_text_chunks() -> None:
    provider = OpenRouterProvider(ModelConfig(provider="openrouter", name="test/model", temperature=0.1), Path(".env"))
    provider.api_key = "secret"

    response = SimpleNamespace(
        raise_for_status=lambda: None,
        iter_lines=lambda: iter([
            'data: {"choices":[{"delta":{"content":"Hel"}}]}',
            'data: {"choices":[{"delta":{"content":"lo"}}]}',
            "data: [DONE]",
        ]),
    )
    class ContextManager:
        def __enter__(self) -> SimpleNamespace:
            return response

        def __exit__(self, exc_type, exc, tb) -> bool:
            return False

    with patch("pulse.provider.httpx.stream", return_value=ContextManager()):
        chunks = list(provider.stream_chat([ChatMessage(role="user", content="hi")]))

    assert chunks == ["Hel", "lo"]


def test_stream_chat_sends_configured_max_tokens() -> None:
    provider = OpenRouterProvider(ModelConfig(provider="openrouter", name="test/model", temperature=0.1, max_tokens=1234), Path(".env"))
    provider.api_key = "secret"
    response = SimpleNamespace(raise_for_status=lambda: None, iter_lines=lambda: iter(["data: [DONE]"]))

    class ContextManager:
        def __enter__(self):
            return response

        def __exit__(self, exc_type, exc, tb):
            return False

    with patch("pulse.provider.httpx.stream", return_value=ContextManager()) as stream:
        provider.stream_chat([ChatMessage(role="user", content="hi")])

    assert stream.call_args.kwargs["json"]["max_tokens"] == 1234


def test_http_error_detail_uses_openrouter_json_message() -> None:
    provider = OpenRouterProvider(ModelConfig(provider="openrouter", name="test/model", temperature=0.1), Path(".env"))
    request = httpx.Request("POST", "https://openrouter.ai/api/v1/chat/completions")
    response = httpx.Response(402, json={"error": {"message": "Insufficient credits"}}, request=request)
    error = httpx.HTTPStatusError("payment required", request=request, response=response)

    assert provider._safe_error_detail(error) == "Insufficient credits"
