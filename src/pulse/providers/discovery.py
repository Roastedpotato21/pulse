from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from pulse.providers.manager import PROVIDER_SPECS

_MODEL_ENDPOINTS = {
    "gemini": "https://generativelanguage.googleapis.com/v1beta/models",
    "openrouter": "https://openrouter.ai/api/v1/models",
    "openai": "https://api.openai.com/v1/models",
    "anthropic": "https://api.anthropic.com/v1/models",
    "groq": "https://api.groq.com/openai/v1/models",
    "deepseek": "https://api.deepseek.com/models",
}


class ModelDiscoveryError(RuntimeError):
    """A safe, credential-free error raised while discovering account models."""

    def __init__(self, message: str, *, status_code: int = 0) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class DiscoveredModel:
    name: str


def detect_provider_candidates(api_key: str) -> tuple[str, ...]:
    """Infer only distinctive key formats without sending the secret anywhere."""
    secret = api_key.strip()
    if secret.startswith("sk-ant-"):
        return ("anthropic",)
    if secret.startswith("gsk_"):
        return ("groq",)
    if secret.startswith("sk-or-v1-"):
        return ("openrouter",)
    if secret.startswith("AIza"):
        return ("gemini",)
    if secret.startswith(("sk-proj-", "sk-svcacct-")):
        return ("openai",)
    if secret.startswith("sk-"):
        return ("openai", "deepseek")
    return ()


def discover_models(
    provider_name: str,
    api_key: str,
    *,
    timeout_seconds: float = 15.0,
) -> list[DiscoveredModel]:
    provider = provider_name.lower().strip()
    if provider not in _MODEL_ENDPOINTS:
        raise ValueError(f"Unsupported provider: '{provider_name}'.")
    if not api_key.strip():
        raise ModelDiscoveryError("An API key is required to discover models.", status_code=401)

    headers = _discovery_headers(provider, api_key.strip())
    params = {"key": api_key.strip()} if provider == "gemini" else None
    try:
        response = httpx.get(
            _MODEL_ENDPOINTS[provider],
            headers=headers,
            params=params,
            timeout=timeout_seconds,
            follow_redirects=False,
        )
        response.raise_for_status()
        data = response.json()
    except httpx.HTTPStatusError as error:
        status = error.response.status_code
        if status in {401, 403}:
            detail = "The provider rejected this API key."
        elif status == 429:
            detail = "The provider rate-limited model discovery."
        else:
            detail = "The provider could not list models for this account."
        raise ModelDiscoveryError(detail, status_code=status) from error
    except (httpx.HTTPError, ValueError) as error:
        raise ModelDiscoveryError(
            "Model discovery could not reach the provider.", status_code=0
        ) from error

    names = _parse_model_names(provider, data)
    return [DiscoveredModel(name=name) for name in names]


def select_preferred_model(
    provider_name: str,
    discovered: list[DiscoveredModel],
    *,
    excluded: set[str] | None = None,
) -> str:
    """Choose the first curated compatible model actually visible to this key."""
    provider = provider_name.lower().strip()
    spec = PROVIDER_SPECS[provider]
    unavailable = {name.lower() for name in (excluded or set())}
    visible = {model.name.lower(): model.name for model in discovered}
    for preferred in spec.available_models:
        key = preferred.name.lower()
        if (
            provider == "openrouter"
            and spec.default_model.endswith(":free")
            and not preferred.name.endswith(":free")
        ):
            continue
        if key in visible and key not in unavailable:
            return visible[key]
    raise ModelDiscoveryError(
        f"No supported {spec.display_name} text model is available to this account."
    )


def _discovery_headers(provider: str, api_key: str) -> dict[str, str]:
    if provider == "gemini":
        return {"Accept": "application/json"}
    if provider == "anthropic":
        return {
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "Accept": "application/json",
        }
    return {"Authorization": f"Bearer {api_key}", "Accept": "application/json"}


def _parse_model_names(provider: str, data: Any) -> list[str]:
    if not isinstance(data, dict):
        raise ModelDiscoveryError("The provider returned an invalid model list.")
    records = data.get("models" if provider == "gemini" else "data", [])
    if not isinstance(records, list):
        raise ModelDiscoveryError("The provider returned an invalid model list.")

    result: list[str] = []
    for record in records:
        if not isinstance(record, dict):
            continue
        if provider == "gemini":
            methods = record.get("supportedGenerationMethods", [])
            if "generateContent" not in methods:
                continue
            name = record.get("name", "")
            if isinstance(name, str) and name.startswith("models/"):
                name = name.removeprefix("models/")
        else:
            name = record.get("id", "")
        if isinstance(name, str) and name and name not in result:
            result.append(name)
    return result
