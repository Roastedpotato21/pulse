from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pulse.config import ModelConfig
from pulse.providers.anthropic import AnthropicProvider
from pulse.providers.base import BaseProvider
from pulse.providers.deepseek import DeepSeekProvider
from pulse.providers.gemini import GeminiProvider
from pulse.providers.groq import GroqProvider
from pulse.providers.openai import OpenAIProvider
from pulse.providers.openrouter import OpenRouterProvider


@dataclass(frozen=True)
class ModelMetadata:
    name: str
    speed: str          # "Fast", "Balanced", "High Quality", "Ultra-Fast"
    context_length: str # "128k", "200k", "1M", "2M", etc.
    best_for: str       # "Coding", "Reasoning", "General", "Vision"
    category: str       # "Flagship", "Coding", "Reasoning", "Fast"
    status: str = "Active"  # "Active", "Deprecated", "Beta"


@dataclass(frozen=True)
class ProviderSpec:
    key: str
    display_name: str
    env_var: str
    provider_class: type[BaseProvider]
    default_model: str
    available_models: list[ModelMetadata]


@dataclass(frozen=True)
class ProviderSelection:
    provider: str
    model: str
    selection_mode: str = "manual"
    schema_version: int = 2


PROVIDER_SPECS: dict[str, ProviderSpec] = {
    "gemini": ProviderSpec(
        key="gemini",
        display_name="Google Gemini",
        env_var="GEMINI_API_KEY",
        provider_class=GeminiProvider,
        default_model="gemini-3.6-flash",
        available_models=[
            ModelMetadata(
                "gemini-3.6-flash",
                "Fast",
                "1M",
                "Reliable Coding & Agentic Workflows",
                "Flagship",
            ),
            ModelMetadata(
                "gemini-3.8-flash",
                "High Quality",
                "1M",
                "Software Engineering & Agentic Workflows",
                "Flagship",
            ),
            ModelMetadata(
                "gemini-3.5-flash",
                "Fast",
                "1M",
                "General Coding & High-Throughput Tasks",
                "Fast",
            ),
            ModelMetadata(
                "gemini-3.5-flash-lite",
                "Ultra-Fast",
                "1M",
                "Lightweight Coding & High Throughput",
                "Fast",
            ),
        ],
    ),
    "openrouter": ProviderSpec(
        key="openrouter",
        display_name="OpenRouter",
        env_var="OPENROUTER_API_KEY",
        provider_class=OpenRouterProvider,
        default_model="cohere/north-mini-code:free",
        available_models=[
            ModelMetadata("cohere/north-mini-code:free", "Free", "256k", "Coding & Refactoring", "Coding"),
            ModelMetadata("poolside/laguna-s-2.1:free", "Free", "256k", "Agentic Coding", "Coding"),
            ModelMetadata("qwen/qwen3.8-flash", "Fast", "1M", "Coding & Agentic Workflows", "Fast"),
            ModelMetadata("google/gemini-3.8-flash", "Fast", "1M", "Software Engineering", "Flagship"),
            ModelMetadata("anthropic/claude-sonnet-4.6", "High Quality", "1M", "Architecture & Technical Writing", "Flagship"),
            ModelMetadata("deepseek/deepseek-v4-pro", "High Quality", "1M", "Complex Coding & Reasoning", "Reasoning"),
        ],
    ),
    "openai": ProviderSpec(
        key="openai",
        display_name="OpenAI",
        env_var="OPENAI_API_KEY",
        provider_class=OpenAIProvider,
        default_model="gpt-5.6-terra",
        available_models=[
            ModelMetadata("gpt-5.6-terra", "Balanced", "256k", "Coding & General Agentic Work", "Flagship"),
            ModelMetadata("gpt-5.6-sol", "High Quality", "256k", "Complex Coding & Reasoning", "Flagship"),
            ModelMetadata("gpt-5.6-luna", "Fast", "256k", "Fast Coding & Iteration", "Fast"),
            ModelMetadata("gpt-4o-mini", "Fast", "128k", "Lightweight Code & Fast Chat", "Fast"),
        ],
    ),
    "anthropic": ProviderSpec(
        key="anthropic",
        display_name="Anthropic",
        env_var="ANTHROPIC_API_KEY",
        provider_class=AnthropicProvider,
        default_model="claude-sonnet-4-6",
        available_models=[
            ModelMetadata("claude-sonnet-4-6", "High Quality", "200k", "Coding & Design", "Flagship"),
            ModelMetadata("claude-haiku-4-5-20251001", "Fast", "200k", "Rapid Refactoring & Lightweight Tasks", "Fast"),
            ModelMetadata("claude-opus-4-8", "High Quality", "200k", "Complex Analysis & Architecture", "Reasoning"),
        ],
    ),
    "groq": ProviderSpec(
        key="groq",
        display_name="Groq",
        env_var="GROQ_API_KEY",
        provider_class=GroqProvider,
        default_model="openai/gpt-oss-120b",
        available_models=[
            ModelMetadata("openai/gpt-oss-120b", "Ultra-Fast", "128k", "Coding & Reasoning", "Flagship"),
            ModelMetadata("openai/gpt-oss-20b", "Ultra-Fast", "128k", "Fast Coding & Iteration", "Fast"),
            ModelMetadata("qwen/qwen3.6-27b", "Fast", "128k", "Coding & General Tasks", "General", "Beta"),
            ModelMetadata("qwen/qwen3.8-27b", "Fast", "128k", "Coding & Reasoning", "General", "Beta"),
        ],
    ),
    "deepseek": ProviderSpec(
        key="deepseek",
        display_name="DeepSeek",
        env_var="DEEPSEEK_API_KEY",
        provider_class=DeepSeekProvider,
        default_model="deepseek-v4-flash",
        available_models=[
            ModelMetadata("deepseek-v4-flash", "Fast", "128k", "General Assistant & Coding", "Flagship"),
            ModelMetadata("deepseek-v4-pro", "High Quality", "128k", "Complex Coding & Reasoning", "Reasoning"),
        ],
    ),
}


RETIRED_MODEL_REPLACEMENTS: dict[str, dict[str, str]] = {
    "gemini": {
        "gemini-1.5-flash": "gemini-3.6-flash",
        "gemini-1.5-pro": "gemini-3.6-flash",
        "gemini-2.0-flash": "gemini-3.6-flash",
        "gemini-2.0-flash-lite": "gemini-3.5-flash-lite",
    },
    "openai": {"o1": "gpt-5.6-terra", "o3-mini": "gpt-5.6-terra"},
    "openrouter": {
        "qwen/qwen3-coder:free": "cohere/north-mini-code:free",
        "anthropic/claude-3.5-sonnet": "anthropic/claude-sonnet-4.6",
        "google/gemini-2.0-flash-001": "google/gemini-3.8-flash",
    },
    "anthropic": {
        "claude-3-5-sonnet-20241022": "claude-sonnet-4-6",
        "claude-3-5-haiku-20241022": "claude-haiku-4-5-20251001",
        "claude-3-opus-20240229": "claude-opus-4-8",
    },
    "groq": {
        "llama-3.3-70b-versatile": "openai/gpt-oss-120b",
        "llama-3.1-8b-instant": "openai/gpt-oss-20b",
        "mixtral-8x7b-32768": "openai/gpt-oss-20b",
        "deepseek-r1-distill-llama-70b": "openai/gpt-oss-120b",
    },
    "deepseek": {
        "deepseek-chat": "deepseek-v4-flash",
        "deepseek-reasoner": "deepseek-v4-pro",
    },
}


class ProviderManager:
    """Central manager for Pulse single-active-model provider selection & configuration."""

    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace.resolve()
        self.config_file = self.workspace / ".agent" / "provider.json"
        try:
            self.config_file.resolve().relative_to(self.workspace)
        except ValueError as error:
            raise ValueError("Provider configuration must remain inside the workspace.") from error

    def list_providers(self) -> list[dict[str, Any]]:
        from pulse.provider_keys import ProviderKeyStore

        statuses = {
            status.provider: status
            for status in ProviderKeyStore(self.workspace).statuses()
        }
        result = []
        for spec in PROVIDER_SPECS.values():
            result.append(
                {
                    "key": spec.key,
                    "display_name": spec.display_name,
                    "env_var": spec.env_var,
                    "configured": statuses[spec.key].configured,
                    "key_source": statuses[spec.key].source,
                    "default_model": spec.default_model,
                    "models": spec.available_models,
                }
            )
        return result

    def get_provider_spec(self, provider_name: str) -> ProviderSpec:
        normalized = provider_name.lower().strip()
        if normalized not in PROVIDER_SPECS:
            supported = ", ".join(PROVIDER_SPECS.keys())
            raise ValueError(
                f"Unsupported provider: '{provider_name}'. Supported providers: {supported}"
            )
        return PROVIDER_SPECS[normalized]

    def get_model_metadata(
        self, provider_name: str, model_name: str
    ) -> ModelMetadata | None:
        try:
            spec = self.get_provider_spec(provider_name)
            for m in spec.available_models:
                if m.name.lower() == model_name.lower():
                    return m
            return ModelMetadata(
                name=model_name,
                speed="Custom",
                context_length="128k",
                best_for="User-Specified Model",
                category="Custom",
            )
        except ValueError:
            return None

    def get_selection(self) -> ProviderSelection:
        """Load the active selection, treating legacy files as explicit/manual choices."""
        if self.config_file.exists():
            if self.config_file.is_symlink() or self.config_file.stat().st_size > 1_048_576:
                return self._default_selection()
            try:
                data = json.loads(self.config_file.read_text(encoding="utf-8"))
                provider = data.get("provider")
                model = data.get("model")
                if provider and model and provider.lower() in PROVIDER_SPECS:
                    mode = data.get("selection_mode", "manual")
                    if mode not in {"auto", "manual"}:
                        mode = "manual"
                    return ProviderSelection(
                        provider=provider.lower(),
                        model=model,
                        selection_mode=mode,
                        schema_version=int(data.get("schema_version", 1)),
                    )
            # Intentionally broad to isolate execution boundaries and prevent crashes.
            except Exception:  # noqa: BLE001, S110
                pass
        return self._default_selection()

    @staticmethod
    def _default_selection() -> ProviderSelection:
        return ProviderSelection(
            provider="openrouter",
            model=PROVIDER_SPECS["openrouter"].default_model,
            selection_mode="auto",
        )

    def get_active_selection(self) -> tuple[str, str]:
        selection = self.get_selection()
        return selection.provider, selection.model

    def get_selection_mode(self) -> str:
        return self.get_selection().selection_mode

    def validate_active_selection(self) -> tuple[str, str, str | None]:
        """Check active selection and fallback gracefully if model is deprecated or unsupported."""
        provider, model = self.get_active_selection()
        try:
            spec = self.get_provider_spec(provider)
        except ValueError:
            default_spec = PROVIDER_SPECS["openrouter"]
            self.save_selection("openrouter", default_spec.default_model)
            return "openrouter", default_spec.default_model, f"Unknown provider '{provider}'. Switched to openrouter:{default_spec.default_model}."

        replacement = RETIRED_MODEL_REPLACEMENTS.get(provider, {}).get(model.lower())
        if replacement:
            warning = (
                f"Selected model '{model}' has been retired by provider '{provider}'. "
                f"Switched to '{replacement}'."
            )
            self.save_selection(
                provider,
                replacement,
                selection_mode=self.get_selection_mode(),
            )
            return provider, replacement, warning

        meta = self.get_model_metadata(provider, model)
        if meta and meta.status.lower() == "deprecated":
            warning = f"Selected model '{model}' is deprecated for provider '{provider}'. Falling back to default model '{spec.default_model}'."
            self.save_selection(
                provider,
                spec.default_model,
                selection_mode=self.get_selection_mode(),
            )
            return provider, spec.default_model, warning

        return provider, model, None

    def save_selection(
        self,
        provider_name: str,
        model_name: str | None = None,
        *,
        selection_mode: str = "manual",
    ) -> tuple[str, str]:
        spec = self.get_provider_spec(provider_name)
        if selection_mode not in {"auto", "manual"}:
            raise ValueError("Selection mode must be 'auto' or 'manual'.")
        selected_model = (model_name or spec.default_model).strip()
        if not selected_model or len(selected_model) > 256 or any(
            ord(character) < 32 for character in selected_model
        ):
            raise ValueError("Model identifiers must be 1-256 characters without controls.")

        self.config_file.parent.mkdir(parents=True, exist_ok=True)
        if self.config_file.parent.is_symlink() or self.config_file.is_symlink():
            raise ValueError("Refusing to write provider configuration through a symbolic link.")
        data = {
            "schema_version": 2,
            "provider": spec.key,
            "model": selected_model,
            "selection_mode": selection_mode,
        }
        temporary_name: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                newline="\n",
                prefix=".provider-",
                suffix=".tmp",
                dir=self.config_file.parent,
                delete=False,
            ) as temporary:
                temporary_name = temporary.name
                json.dump(data, temporary, indent=2)
                temporary.write("\n")
            os.replace(temporary_name, self.config_file)
            temporary_name = None
        finally:
            if temporary_name:
                Path(temporary_name).unlink(missing_ok=True)
        return spec.key, selected_model

    def resolve_auto_model(
        self,
        provider_name: str,
        api_key: str,
        *,
        excluded: set[str] | None = None,
        persist: bool = True,
    ) -> str:
        """Discover and select a compatible model visible to the supplied account."""
        from pulse.providers.discovery import discover_models, select_preferred_model

        spec = self.get_provider_spec(provider_name)
        model = select_preferred_model(
            spec.key,
            discover_models(spec.key, api_key),
            excluded=excluded,
        )
        if persist:
            self.save_selection(spec.key, model, selection_mode="auto")
        return model

    def create_provider(
        self,
        config: ModelConfig,
        workspace_env_path: Path | str | None = None,
        api_key: str | None = None,
    ) -> BaseProvider:
        env_path = workspace_env_path or (self.workspace / ".env")
        spec = self.get_provider_spec(config.provider)
        return spec.provider_class(
            config=config, workspace_env_path=env_path, api_key=api_key
        )
