from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pulse.config import ModelConfig, load_env_file
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


PROVIDER_SPECS: dict[str, ProviderSpec] = {
    "gemini": ProviderSpec(
        key="gemini",
        display_name="Google Gemini",
        env_var="GEMINI_API_KEY",
        provider_class=GeminiProvider,
        default_model="gemini-2.0-flash",
        available_models=[
            ModelMetadata("gemini-2.0-flash", "Fast", "1M", "General & Fast Coding", "Flagship"),
            ModelMetadata("gemini-1.5-pro", "High Quality", "2M", "Reasoning & Deep Analysis", "Reasoning"),
            ModelMetadata("gemini-1.5-flash", "Ultra-Fast", "1M", "Lightweight Coding & Speed", "Fast"),
        ],
    ),
    "openrouter": ProviderSpec(
        key="openrouter",
        display_name="OpenRouter",
        env_var="OPENROUTER_API_KEY",
        provider_class=OpenRouterProvider,
        default_model="qwen/qwen3-coder",
        available_models=[
            ModelMetadata("qwen/qwen3-coder", "Balanced", "128k", "Advanced Coding & Refactoring", "Coding"),
            ModelMetadata("anthropic/claude-3.5-sonnet", "High Quality", "200k", "Architecture & Technical Writing", "Flagship"),
            ModelMetadata("deepseek/deepseek-r1", "High Quality", "164k", "Complex Reasoning & STEM", "Reasoning"),
            ModelMetadata("google/gemini-2.0-flash-001", "Fast", "1M", "Fast Code Generation", "Fast"),
            ModelMetadata("meta-llama/llama-3.3-70b-instruct", "Balanced", "128k", "General & Open Source", "General"),
        ],
    ),
    "openai": ProviderSpec(
        key="openai",
        display_name="OpenAI",
        env_var="OPENAI_API_KEY",
        provider_class=OpenAIProvider,
        default_model="gpt-4o",
        available_models=[
            ModelMetadata("gpt-4o", "High Quality", "128k", "Multimodal, Architecture & Coding", "Flagship"),
            ModelMetadata("gpt-4o-mini", "Fast", "128k", "Lightweight Code & Fast Chat", "Fast"),
            ModelMetadata("o1", "High Quality", "200k", "STEM & Complex Reasoning", "Reasoning"),
            ModelMetadata("o3-mini", "Fast", "200k", "Fast Technical Reasoning & Math", "Reasoning"),
        ],
    ),
    "anthropic": ProviderSpec(
        key="anthropic",
        display_name="Anthropic",
        env_var="ANTHROPIC_API_KEY",
        provider_class=AnthropicProvider,
        default_model="claude-3-5-sonnet-20241022",
        available_models=[
            ModelMetadata("claude-3-5-sonnet-20241022", "High Quality", "200k", "State-of-the-Art Coding & Design", "Flagship"),
            ModelMetadata("claude-3-5-haiku-20241022", "Fast", "200k", "Rapid Refactoring & Lightweight Tasks", "Fast"),
            ModelMetadata("claude-3-opus-20240229", "High Quality", "200k", "Complex Analysis & System Architecture", "Reasoning"),
        ],
    ),
    "groq": ProviderSpec(
        key="groq",
        display_name="Groq",
        env_var="GROQ_API_KEY",
        provider_class=GroqProvider,
        default_model="llama-3.3-70b-versatile",
        available_models=[
            ModelMetadata("llama-3.3-70b-versatile", "Ultra-Fast", "128k", "General & Fast Coding", "Flagship"),
            ModelMetadata("llama-3.1-8b-instant", "Ultra-Fast", "128k", "Instant Search & Micro-Edits", "Fast"),
            ModelMetadata("mixtral-8x7b-32768", "Fast", "32k", "Fast Instruction Following", "General"),
            ModelMetadata("deepseek-r1-distill-llama-70b", "Fast", "128k", "Fast STEM Reasoning", "Reasoning"),
        ],
    ),
    "deepseek": ProviderSpec(
        key="deepseek",
        display_name="DeepSeek",
        env_var="DEEPSEEK_API_KEY",
        provider_class=DeepSeekProvider,
        default_model="deepseek-chat",
        available_models=[
            ModelMetadata("deepseek-chat", "Balanced", "64k", "General Assistant & Coding (V3)", "Flagship"),
            ModelMetadata("deepseek-reasoner", "High Quality", "64k", "Chain-of-Thought Reasoning (R1)", "Reasoning"),
        ],
    ),
}


class ProviderManager:
    """Central manager for Pulse single-active-model provider selection & configuration."""

    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace.resolve()
        self.config_file = self.workspace / ".agent" / "provider.json"

    def list_providers(self) -> list[dict[str, Any]]:
        env_file = self.workspace / ".env"
        env = load_env_file(env_file) if env_file.exists() else {}
        result = []
        for spec in PROVIDER_SPECS.values():
            key_val = os.environ.get(spec.env_var) or env.get(spec.env_var)
            is_configured = bool(
                key_val and key_val.strip() and key_val != "replace_me"
            )
            result.append(
                {
                    "key": spec.key,
                    "display_name": spec.display_name,
                    "env_var": spec.env_var,
                    "configured": is_configured,
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

    def get_active_selection(self) -> tuple[str, str]:
        """Return (provider_name, model_name) stored in .agent/provider.json or default."""
        if self.config_file.exists():
            try:
                data = json.loads(self.config_file.read_text(encoding="utf-8"))
                provider = data.get("provider")
                model = data.get("model")
                if provider and model and provider.lower() in PROVIDER_SPECS:
                    return provider.lower(), model
            # Intentionally broad to isolate execution boundaries and prevent crashes.
            except Exception:  # noqa: BLE001, S110
                pass
        return "openrouter", PROVIDER_SPECS["openrouter"].default_model

    def validate_active_selection(self) -> tuple[str, str, str | None]:
        """Check active selection and fallback gracefully if model is deprecated or unsupported."""
        provider, model = self.get_active_selection()
        try:
            spec = self.get_provider_spec(provider)
        except ValueError:
            default_spec = PROVIDER_SPECS["openrouter"]
            self.save_selection("openrouter", default_spec.default_model)
            return "openrouter", default_spec.default_model, f"Unknown provider '{provider}'. Switched to openrouter:{default_spec.default_model}."

        meta = self.get_model_metadata(provider, model)
        if meta and meta.status.lower() == "deprecated":
            warning = f"Selected model '{model}' is deprecated for provider '{provider}'. Falling back to default model '{spec.default_model}'."
            self.save_selection(provider, spec.default_model)
            return provider, spec.default_model, warning

        return provider, model, None

    def save_selection(
        self, provider_name: str, model_name: str | None = None
    ) -> tuple[str, str]:
        spec = self.get_provider_spec(provider_name)
        selected_model = (model_name or spec.default_model).strip()

        self.config_file.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "schema_version": 1,
            "provider": spec.key,
            "model": selected_model,
        }
        self.config_file.write_text(json.dumps(data, indent=2), encoding="utf-8")
        return spec.key, selected_model

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
