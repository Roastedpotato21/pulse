import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pulse.config import ModelConfig
from pulse.provider import GeminiProvider, ProviderFactory


class DummyStreamResponse:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def aiter_lines(self):
        yield 'data: {"candidates":[{"content":{"parts":[{"text":"Hello"}]}}]}'
        yield 'data: {"candidates":[{"content":{"parts":[{"text":" world"}]}}]}'


class DummyClient:
    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def stream(self, method, url, **kwargs):
        return DummyStreamResponse()


def test_provider_factory_creates_gemini_provider() -> None:
    factory = ProviderFactory()
    provider = factory.create("gemini", ModelConfig(provider="gemini", name="gemini-2.0", temperature=0.2), Path(".env"))

    assert isinstance(provider, GeminiProvider)


def test_gemini_provider_streams_chunks() -> None:
    provider = GeminiProvider(ModelConfig(provider="gemini", name="gemini-2.0", temperature=0.2), Path(".env"), api_key="secret")

    with patch("pulse.provider.httpx.AsyncClient", DummyClient):
        chunks = asyncio.run(
            _collect_chunks(provider)
        )

    assert [chunk.content for chunk in chunks] == ["Hello", " world"]


async def _collect_chunks(provider: GeminiProvider) -> list:
    collected = []
    async for chunk in provider.generate_stream([{"role": "user", "content": "hi"}], temperature=0.1):
        collected.append(chunk)
    return collected
