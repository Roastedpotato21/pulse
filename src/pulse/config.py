from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_MAX_ENV_FILE_BYTES = 1_048_576
_MAX_CONFIG_FILE_BYTES = 1_048_576
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _workspace_path(workspace: Path, value: object, label: str) -> Path:
    candidate = (workspace / str(value)).resolve()
    try:
        candidate.relative_to(workspace.resolve())
    except ValueError as error:
        raise ValueError(f"{label} must remain inside the workspace.") from error
    return candidate


@dataclass(frozen=True)
class ModelConfig:
    provider: str
    name: str
    temperature: float
    max_tokens: int = 8192


@dataclass(frozen=True)
class SandboxConfig:
    workspace_root: Path
    require_permission_for_reads: bool = True
    require_permission_for_project_actions: bool = True
    allow_writes: bool = False


@dataclass(frozen=True)
class LoggingConfig:
    action_log: Path
    telemetry_log: Path | None = None


@dataclass(frozen=True)
class AgentConfig:
    agent_name: str
    mode: str
    model: ModelConfig
    sandbox: SandboxConfig
    logging: LoggingConfig


def load_agent_config(workspace: Path) -> AgentConfig:
    raw = _read_json(workspace / "agent.config.json")
    orig_env_provider = os.environ.get("AGENT_PROVIDER")
    orig_env_model = os.environ.get("AGENT_MODEL")

    env = load_env_file(workspace / ".env")

    model_raw = raw.get("model", {})
    sandbox_raw = raw.get("sandbox", {})
    logging_raw = raw.get("logging", {})

    from pulse.providers.manager import ProviderManager

    pm = ProviderManager(workspace)
    saved_provider, saved_model = pm.get_active_selection()

    if orig_env_provider and orig_env_model:
        provider = orig_env_provider
        model_name = orig_env_model
    elif (workspace / ".agent" / "provider.json").exists():
        provider = saved_provider
        model_name = saved_model
    else:
        provider = (
            orig_env_provider
            or os.environ.get("AGENT_PROVIDER")
            or model_raw.get("provider")
            or saved_provider
        )
        model_name = (
            orig_env_model
            or os.environ.get("AGENT_MODEL")
            or model_raw.get("name")
            or saved_model
        )

    max_tokens = int(
        os.environ.get("AGENT_MAX_TOKENS")
        or env.get("AGENT_MAX_TOKENS")
        or model_raw.get("maxTokens", 8192)
    )
    if max_tokens <= 0:
        raise ValueError("Model maxTokens must be a positive integer.")

    return AgentConfig(
        agent_name=raw.get("agentName", "Kiran"),
        mode=raw.get("mode", "single-model"),
        model=ModelConfig(
            provider=provider,
            name=model_name,
            temperature=float(model_raw.get("temperature", 0.2)),
            max_tokens=max_tokens,
        ),
        sandbox=SandboxConfig(
            workspace_root=_workspace_path(
                workspace, sandbox_raw.get("workspaceRoot", "."), "sandbox.workspaceRoot"
            ),
            require_permission_for_reads=bool(
                sandbox_raw.get("requirePermissionForReads", True)
            ),
            require_permission_for_project_actions=bool(
                sandbox_raw.get("requirePermissionForProjectActions", True)
            ),
            allow_writes=bool(sandbox_raw.get("allowWrites", False)),
        ),
        logging=LoggingConfig(
            action_log=_workspace_path(
                workspace,
                logging_raw.get("actionLog", ".agent/logs/actions.jsonl"),
                "logging.actionLog",
            ),
            telemetry_log=_workspace_path(
                workspace,
                logging_raw.get("telemetryLog", ".agent/logs/telemetry.jsonl"),
                "logging.telemetryLog",
            ),
        ),
    )


def load_env_file(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    if path.is_symlink():
        raise ValueError("Refusing to load a symbolic-link .env file.")
    if path.stat().st_size > _MAX_ENV_FILE_BYTES:
        raise ValueError("The .env file exceeds the 1 MiB safety limit.")

    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue

        key, value = stripped.split("=", 1)
        k = key.strip()
        if not _ENV_NAME.fullmatch(k):
            continue
        v = value.strip().strip("\"'")
        values[k] = v

    return values


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    if path.is_symlink():
        raise ValueError(f"Refusing to load symbolic-link configuration: {path.name}")
    if path.stat().st_size > _MAX_CONFIG_FILE_BYTES:
        raise ValueError(f"Configuration file exceeds the 1 MiB safety limit: {path.name}")

    return json.loads(path.read_text(encoding="utf-8"))
