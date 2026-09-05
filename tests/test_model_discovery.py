from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from pulse.config import ModelConfig
from pulse.core.protocols import StreamChunk
from pulse.providers.auto import AutoModelProvider
from pulse.providers.base import ProviderRequestError
from pulse.providers.discovery import (
    DiscoveredModel,
    ModelDiscoveryError,
    detect_provider_candidates,
    discover_models,
    select_preferred_model,
)


class FakeResponse:
    def __init__(self, data: dict[str, object], status_code: int = 200) -> None:
        self._data = data
        self.status_code = status_code
        self.request = httpx.Request("GET", "https://provider.invalid/models")

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                "failed", request=self.request, response=self  # type: ignore[arg-type]
            )

    def json(self) -> dict[str, object]:
        return self._data


def test_key_detection_is_local_and_only_claims_distinctive_formats() -> None:
    assert detect_provider_candidates("AIza-example") == ("gemini",)
    assert detect_provider_candidates("sk-ant-example") == ("anthropic",)
    assert detect_provider_candidates("gsk_example") == ("groq",)
    assert detect_provider_candidates("sk-or-v1-example") == ("openrouter",)
    assert detect_provider_candidates("sk-example") == ("openai", "deepseek")
    assert detect_provider_candidates("random") == ()


def test_gemini_discovery_filters_non_generation_models(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "pulse.providers.discovery.httpx.get",
        lambda *_args, **_kwargs: FakeResponse(
            {
                "models": [
                    {
                        "name": "models/gemini-3.6-flash",
                        "supportedGenerationMethods": ["generateContent"],
                    },
                    {
                        "name": "models/text-embedding-004",
                        "supportedGenerationMethods": ["embedContent"],
                    },
                ]
            }
        ),
    )

    models = discover_models("gemini", "synthetic-key")

    assert [model.name for model in models] == ["gemini-3.6-flash"]


@pytest.mark.parametrize(
    ("provider", "model"),
    [
        ("openai", "gpt-5.6-terra"),
        ("anthropic", "claude-sonnet-4-6"),
        ("groq", "openai/gpt-oss-120b"),
        ("deepseek", "deepseek-v4-flash"),
        ("openrouter", "cohere/north-mini-code:free"),
    ],
)
def test_openai_shaped_model_lists_are_parsed_for_each_provider(
    provider: str,
    model: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "pulse.providers.discovery.httpx.get",
        lambda *_args, **_kwargs: FakeResponse({"data": [{"id": model}]}),
    )

    assert discover_models(provider, "synthetic-key") == [DiscoveredModel(model)]
    assert select_preferred_model(provider, [DiscoveredModel(model)]) == model


def test_discovery_error_never_contains_the_key(monkeypatch: pytest.MonkeyPatch) -> None:
    secret = "sk-secret-that-must-not-leak"
    monkeypatch.setattr(
        "pulse.providers.discovery.httpx.get",
        lambda *_args, **_kwargs: FakeResponse({}, status_code=401),
    )

    with pytest.raises(ModelDiscoveryError) as exc_info:
        discover_models("openai", secret)

    assert secret not in str(exc_info.value)


def test_openrouter_auto_never_falls_through_from_free_to_paid() -> None:
    with pytest.raises(ModelDiscoveryError, match="No supported"):
        select_preferred_model(
            "openrouter",
            [DiscoveredModel("anthropic/claude-sonnet-4.6")],
        )


class FakeStreamingProvider:
    def __init__(self, model: str, events: list[object]) -> None:
        self.config = ModelConfig(provider="openai", name=model, temperature=0.2)
        self.api_key = "synthetic-key"
        self.is_configured = True
        self.events = events

    async def generate_stream(self, _messages, _temperature=0.2):  # type: ignore[no-untyped-def]
        for event in self.events:
            if isinstance(event, Exception):
                raise event
            yield event


def test_auto_model_recovers_once_before_any_chunk() -> None:
    unavailable = ProviderRequestError("gone", status_code=404, retryable=False)
    first = FakeStreamingProvider("old-model", [unavailable])
    second = FakeStreamingProvider("new-model", [StreamChunk(content="ok")])
    manager = SimpleNamespace(
        resolve_auto_model=lambda *_args, **_kwargs: "new-model",
        create_provider=lambda *_args, **_kwargs: second,
    )
    provider = AutoModelProvider(manager, first, Path(".env"))  # type: ignore[arg-type]

    async def collect() -> list[str]:
        return [chunk.content async for chunk in provider.generate_stream([])]

    assert asyncio.run(collect()) == ["ok"]
    assert provider.config.name == "new-model"


def test_auto_model_does_not_switch_after_output_was_emitted() -> None:
    unavailable = ProviderRequestError("gone", status_code=404, retryable=False)
    first = FakeStreamingProvider(
        "old-model", [StreamChunk(content="partial"), unavailable]
    )
    manager = SimpleNamespace(
        resolve_auto_model=lambda *_args, **_kwargs: pytest.fail("must not discover"),
        create_provider=lambda *_args, **_kwargs: pytest.fail("must not switch"),
    )
    provider = AutoModelProvider(manager, first, Path(".env"))  # type: ignore[arg-type]

    async def collect() -> None:
        async for _chunk in provider.generate_stream([]):
            pass

    with pytest.raises(ProviderRequestError):
        asyncio.run(collect())


def test_auto_model_does_not_switch_on_authentication_failure() -> None:
    unauthorized = ProviderRequestError("invalid", status_code=401, retryable=False)
    first = FakeStreamingProvider("old-model", [unauthorized])
    manager = SimpleNamespace(
        resolve_auto_model=lambda *_args, **_kwargs: pytest.fail("must not discover"),
        create_provider=lambda *_args, **_kwargs: pytest.fail("must not switch"),
    )
    provider = AutoModelProvider(manager, first, Path(".env"))  # type: ignore[arg-type]

    async def collect() -> None:
        async for _chunk in provider.generate_stream([]):
            pass

    with pytest.raises(ProviderRequestError) as exc_info:
        asyncio.run(collect())
    assert exc_info.value.status_code == 401
