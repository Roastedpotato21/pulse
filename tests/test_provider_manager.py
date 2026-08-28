from __future__ import annotations

import json
from pathlib import Path

import pytest

from pulse.config import ModelConfig
from pulse.provider import ProviderFactory
from pulse.providers import (
    AnthropicProvider,
    DeepSeekProvider,
    GeminiProvider,
    GroqProvider,
    OpenAIProvider,
    OpenRouterProvider,
    ProviderManager,
)


@pytest.fixture
def temp_workspace(tmp_path: Path) -> Path:
    return tmp_path


def test_list_providers(temp_workspace: Path) -> None:
    pm = ProviderManager(temp_workspace)
    providers = pm.list_providers()
    assert len(providers) == 6
    keys = {p["key"] for p in providers}
    assert keys == {"gemini", "openrouter", "openai", "anthropic", "groq", "deepseek"}


def test_save_and_get_active_selection(temp_workspace: Path) -> None:
    pm = ProviderManager(temp_workspace)

    # Initial default selection
    prov, model = pm.get_active_selection()
    assert prov == "openrouter"
    assert model == "qwen/qwen3-coder"

    # Save selection
    saved_p, saved_m = pm.save_selection("gemini", "gemini-1.5-pro")
    assert saved_p == "gemini"
    assert saved_m == "gemini-1.5-pro"

    # Verify JSON persisted
    json_path = temp_workspace / ".agent" / "provider.json"
    assert json_path.exists()
    data = json.loads(json_path.read_text(encoding="utf-8"))
    assert data == {
        "schema_version": 1,
        "provider": "gemini",
        "model": "gemini-1.5-pro",
    }

    # Read back active selection
    read_p, read_m = pm.get_active_selection()
    assert read_p == "gemini"
    assert read_m == "gemini-1.5-pro"


def test_model_metadata_lookup(temp_workspace: Path) -> None:
    pm = ProviderManager(temp_workspace)
    meta = pm.get_model_metadata("anthropic", "claude-3-5-sonnet-20241022")
    assert meta is not None
    assert meta.name == "claude-3-5-sonnet-20241022"
    assert meta.speed == "High Quality"
    assert meta.context_length == "200k"
    assert "Coding" in meta.best_for


def test_validate_active_selection(temp_workspace: Path) -> None:
    pm = ProviderManager(temp_workspace)
    prov, model, warning = pm.validate_active_selection()
    assert prov == "openrouter"
    assert model == "qwen/qwen3-coder"
    assert warning is None


def test_api_key_detection(temp_workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-12345")
    monkeypatch.delenv("GROQ_API_KEY", raising=False)

    pm = ProviderManager(temp_workspace)
    providers = pm.list_providers()
    status_map = {p["key"]: p["configured"] for p in providers}

    assert status_map["anthropic"] is True
    assert status_map["groq"] is False


def test_create_provider_instances(temp_workspace: Path) -> None:
    pm = ProviderManager(temp_workspace)
    config = ModelConfig(provider="anthropic", name="claude-3-5-sonnet-20241022", temperature=0.2)
    provider = pm.create_provider(config, api_key="test-key")

    assert isinstance(provider, AnthropicProvider)
    assert provider.config.name == "claude-3-5-sonnet-20241022"
    assert provider.api_key == "test-key"


def test_unsupported_provider_raises(temp_workspace: Path) -> None:
    pm = ProviderManager(temp_workspace)
    with pytest.raises(ValueError, match="Unsupported provider"):
        pm.get_provider_spec("unsupported_ai")


def test_provider_factory_compatibility(temp_workspace: Path) -> None:
    factory = ProviderFactory()
    env_file = temp_workspace / ".env"

    providers_to_test = [
        ("gemini", GeminiProvider, "gemini-2.0-flash"),
        ("openrouter", OpenRouterProvider, "qwen/qwen3-coder"),
        ("openai", OpenAIProvider, "gpt-4o"),
        ("anthropic", AnthropicProvider, "claude-3-5-sonnet-20241022"),
        ("groq", GroqProvider, "llama-3.3-70b-versatile"),
        ("deepseek", DeepSeekProvider, "deepseek-chat"),
    ]

    for key, cls, model_name in providers_to_test:
        config = ModelConfig(provider=key, name=model_name, temperature=0.2)
        inst = factory.create(key, config, env_file, api_key="dummy-key")
        assert isinstance(inst, cls)
