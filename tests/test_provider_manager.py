from __future__ import annotations

import json
from pathlib import Path

import pytest

from pulse.config import ModelConfig
from pulse.provider import ProviderFactory
from pulse.provider_keys import ProviderKeyStore
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
    assert model == "cohere/north-mini-code:free"

    # Save selection
    saved_p, saved_m = pm.save_selection("gemini", "gemini-3.8-flash")
    assert saved_p == "gemini"
    assert saved_m == "gemini-3.8-flash"

    # Verify JSON persisted
    json_path = temp_workspace / ".agent" / "provider.json"
    assert json_path.exists()
    data = json.loads(json_path.read_text(encoding="utf-8"))
    assert data == {
        "schema_version": 2,
        "provider": "gemini",
        "model": "gemini-3.8-flash",
        "selection_mode": "manual",
    }

    # Read back active selection
    read_p, read_m = pm.get_active_selection()
    assert read_p == "gemini"
    assert read_m == "gemini-3.8-flash"


def test_model_metadata_lookup(temp_workspace: Path) -> None:
    pm = ProviderManager(temp_workspace)
    meta = pm.get_model_metadata("anthropic", "claude-sonnet-4-6")
    assert meta is not None
    assert meta.name == "claude-sonnet-4-6"
    assert meta.speed == "High Quality"
    assert meta.context_length == "200k"
    assert "Coding" in meta.best_for


def test_validate_active_selection(temp_workspace: Path) -> None:
    pm = ProviderManager(temp_workspace)
    prov, model, warning = pm.validate_active_selection()
    assert prov == "openrouter"
    assert model == "cohere/north-mini-code:free"
    assert warning is None


def test_validate_active_selection_migrates_retired_gemini_model(
    temp_workspace: Path,
) -> None:
    pm = ProviderManager(temp_workspace)
    pm.save_selection("gemini", "gemini-1.5-flash")

    provider, model, warning = pm.validate_active_selection()

    assert provider == "gemini"
    assert model == "gemini-3.6-flash"
    assert warning is not None
    assert "retired" in warning
    assert pm.get_active_selection() == ("gemini", "gemini-3.6-flash")


def test_legacy_selection_is_loaded_as_manual(temp_workspace: Path) -> None:
    config_file = temp_workspace / ".agent" / "provider.json"
    config_file.parent.mkdir(parents=True)
    config_file.write_text(
        json.dumps(
            {"schema_version": 1, "provider": "openai", "model": "custom-model"}
        ),
        encoding="utf-8",
    )

    selection = ProviderManager(temp_workspace).get_selection()

    assert selection.provider == "openai"
    assert selection.model == "custom-model"
    assert selection.selection_mode == "manual"


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


def test_create_provider_reads_key_from_native_vault(
    temp_workspace: Path, memory_provider_keyring
) -> None:
    secret = "synthetic-vault-provider-key"
    ProviderKeyStore(temp_workspace).set("anthropic", secret)
    config = ModelConfig(
        provider="anthropic",
        name="claude-3-5-sonnet-20241022",
        temperature=0.2,
    )

    provider = ProviderManager(temp_workspace).create_provider(config)

    assert provider.api_key == secret


def test_unsupported_provider_raises(temp_workspace: Path) -> None:
    pm = ProviderManager(temp_workspace)
    with pytest.raises(ValueError, match="Unsupported provider"):
        pm.get_provider_spec("unsupported_ai")


def test_provider_factory_compatibility(temp_workspace: Path) -> None:
    factory = ProviderFactory()
    env_file = temp_workspace / ".env"

    providers_to_test = [
        ("gemini", GeminiProvider, "gemini-3.6-flash"),
        ("openrouter", OpenRouterProvider, "cohere/north-mini-code:free"),
        ("openai", OpenAIProvider, "gpt-5.6-terra"),
        ("anthropic", AnthropicProvider, "claude-sonnet-4-6"),
        ("groq", GroqProvider, "openai/gpt-oss-120b"),
        ("deepseek", DeepSeekProvider, "deepseek-v4-flash"),
    ]

    for key, cls, model_name in providers_to_test:
        config = ModelConfig(provider=key, name=model_name, temperature=0.2)
        inst = factory.create(key, config, env_file, api_key="dummy-key")
        assert isinstance(inst, cls)
