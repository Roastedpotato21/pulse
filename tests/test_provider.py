from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest

from pulse.config import ModelConfig
from pulse.provider import ChatMessage, OpenRouterProvider
from pulse.providers.base import ProviderRequestError


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


def test_http_error_detail_does_not_reflect_openrouter_json_message() -> None:
    provider = OpenRouterProvider(ModelConfig(provider="openrouter", name="test/model", temperature=0.1), Path(".env"))
    request = httpx.Request("POST", "https://openrouter.ai/api/v1/chat/completions")
    response = httpx.Response(402, json={"error": {"message": "Insufficient credits"}}, request=request)
    error = httpx.HTTPStatusError("payment required", request=request, response=response)

    detail = provider._safe_error_detail(error)
    assert "Insufficient credits" not in detail
    assert "insufficient credits" in detail
    assert "free model" in detail


def test_payment_required_is_non_retryable_and_actionable() -> None:
    provider = OpenRouterProvider(
        ModelConfig(provider="openrouter", name="paid/model", temperature=0.1),
        Path(".env"),
    )
    provider.api_key = "secret"
    request = httpx.Request("POST", provider.endpoint)
    response = httpx.Response(402, json={"error": "private-provider-detail"}, request=request)

    class ContextManager:
        def __enter__(self):
            return response

        def __exit__(self, exc_type, exc, tb):
            return False

    with (
        patch("pulse.providers.base.httpx.stream", return_value=ContextManager()),
        pytest.raises(ProviderRequestError) as exc_info,
    ):
        provider.stream_chat([ChatMessage(role="user", content="hi")])

    assert exc_info.value.status_code == 402
    assert exc_info.value.retryable is False
    assert "private-provider-detail" not in str(exc_info.value)
    assert "select a free model" in str(exc_info.value)
