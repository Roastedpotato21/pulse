from __future__ import annotations

import os
import shutil
import sys
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlparse

from pulse.config import AgentConfig

LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
_WEAK_TOKEN_MARKERS = ("change", "default", "example", "replace", "secret", "test", "token")


@dataclass(frozen=True, slots=True)
class ProductionCheck:
    name: str
    ok: bool
    detail: str
    remediation: str
    blocking: bool = True

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ProductionReport:
    target: str
    checks: tuple[ProductionCheck, ...]

    @property
    def passed(self) -> bool:
        return all(check.ok or not check.blocking for check in self.checks)

    def to_dict(self) -> dict[str, object]:
        return {
            "target": self.target,
            "passed": self.passed,
            "checks": [check.to_dict() for check in self.checks],
        }


def is_secure_remote_token(token: str) -> bool:
    normalized = token.strip().lower()
    return (
        len(token.strip()) >= 32
        and len(set(token.strip())) >= 12
        and not any(marker in normalized for marker in _WEAK_TOKEN_MARKERS)
    )


def _inside(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
    except ValueError:
        return False
    return True


def _integer_check(
    checks: list[ProductionCheck],
    environ: Mapping[str, str],
    name: str,
    default: int,
    minimum: int,
    maximum: int,
) -> int | None:
    raw = environ.get(name, str(default))
    try:
        value = int(raw)
    except ValueError:
        value = None
    ok = value is not None and minimum <= value <= maximum
    checks.append(
        ProductionCheck(
            name,
            ok,
            raw if ok else "invalid integer or outside the supported range",
            f"Set {name} to an integer between {minimum} and {maximum}.",
        )
    )
    return value


def run_production_checks(
    workspace: Path,
    config: AgentConfig,
    *,
    provider_configured: bool,
    target: str = "local",
    environ: Mapping[str, str] | None = None,
) -> ProductionReport:
    if target not in {"local", "remote"}:
        raise ValueError("Production target must be 'local' or 'remote'.")
    env = environ if environ is not None else os.environ
    root = workspace.resolve()
    checks: list[ProductionCheck] = [
        ProductionCheck(
            "python_version",
            sys.version_info >= (3, 11),
            f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
            "Install Python 3.11 or newer.",
        ),
        ProductionCheck(
            "workspace",
            root.is_dir(),
            str(root),
            "Run Pulse from an existing project directory.",
        ),
        ProductionCheck(
            "agent_config",
            (root / "agent.config.json").is_file(),
            "present" if (root / "agent.config.json").is_file() else "missing",
            "Create agent.config.json from the documented production template.",
        ),
        ProductionCheck(
            "provider_credentials",
            provider_configured,
            "configured" if provider_configured else "missing or placeholder",
            "Configure the selected provider credential in the process environment or OS keyring.",
        ),
        ProductionCheck(
            "model_selection",
            bool(config.model.provider.strip() and config.model.name.strip()),
            f"{config.model.provider}/{config.model.name}",
            "Select an explicit provider and model with `pulse model`.",
        ),
        ProductionCheck(
            "workspace_boundary",
            _inside(config.sandbox.workspace_root, root),
            str(config.sandbox.workspace_root),
            "Set sandbox.workspaceRoot to the project directory or one of its descendants.",
        ),
        ProductionCheck(
            "audit_log_boundary",
            _inside(config.logging.action_log, root),
            str(config.logging.action_log),
            "Store the action log inside the protected project state directory.",
        ),
        ProductionCheck(
            "unsafe_host_execution",
            env.get("PULSE_UNSAFE_HOST_EXECUTION", "").lower()
            not in {"1", "true", "yes", "on"},
            "disabled",
            "Unset PULSE_UNSAFE_HOST_EXECUTION; production execution must use Docker or remote isolation.",
        ),
    ]

    pulse_path = shutil.which("pulse")
    checks.append(
        ProductionCheck(
            "installed_cli",
            pulse_path is not None,
            pulse_path or "not found on PATH",
            "Install the reviewed wheel with `uv tool install pulse-coding-agent`.",
        )
    )

    container_runtime = shutil.which("docker") or shutil.which("podman")
    checks.append(
        ProductionCheck(
            "container_runtime",
            container_runtime is not None,
            container_runtime or "not found on PATH",
            "Install Docker or Podman before enabling model-generated command execution.",
            blocking=target == "remote",
        )
    )

    endpoint = env.get("PULSE_REMOTE_URL", "").strip()
    if endpoint:
        parsed = urlparse(endpoint)
        endpoint_ok = parsed.scheme == "wss" or (
            parsed.scheme == "ws" and parsed.hostname in LOOPBACK_HOSTS
        )
        checks.append(
            ProductionCheck(
                "remote_endpoint_transport",
                endpoint_ok,
                f"{parsed.scheme or 'missing scheme'}://{parsed.hostname or 'missing host'}",
                "Use wss:// with mTLS, or ws:// only for a loopback endpoint.",
            )
        )

    if target == "remote":
        host = env.get("PULSE_REMOTE_HOST", "127.0.0.1").strip()
        tokens = [value.strip() for value in env.get("PULSE_REMOTE_TOKEN", "").split(",") if value.strip()]
        checks.append(
            ProductionCheck(
                "remote_tokens",
                bool(tokens) and all(is_secure_remote_token(token) for token in tokens),
                f"{len(tokens)} token(s) configured" if tokens else "missing",
                "Generate independent random remote tokens of at least 32 characters; do not use placeholders.",
            )
        )
        _integer_check(checks, env, "PULSE_REMOTE_PORT", 8080, 1, 65535)
        _integer_check(checks, env, "PULSE_REMOTE_MAX_CONCURRENCY", 10, 1, 128)
        _integer_check(checks, env, "PULSE_REMOTE_RETENTION_HOURS", 24, 1, 8760)

        workspace_root = Path(env.get("PULSE_REMOTE_WORKSPACE_ROOT", ""))
        database_path = Path(env.get("PULSE_REMOTE_DB", ""))
        checks.extend(
            [
                ProductionCheck(
                    "remote_workspace_root",
                    bool(str(workspace_root)) and workspace_root.is_absolute(),
                    str(workspace_root) or "missing",
                    "Set PULSE_REMOTE_WORKSPACE_ROOT to a dedicated absolute volume path.",
                ),
                ProductionCheck(
                    "remote_database",
                    bool(str(database_path)) and database_path.is_absolute(),
                    str(database_path) or "missing",
                    "Set PULSE_REMOTE_DB to an absolute path on a backed-up durable volume.",
                ),
            ]
        )

        if host not in LOOPBACK_HOSTS:
            for variable in ("PULSE_TLS_CERT", "PULSE_TLS_KEY", "PULSE_TLS_CA"):
                value = env.get(variable, "")
                path = Path(value) if value else None
                checks.append(
                    ProductionCheck(
                        variable.lower(),
                        bool(path and path.is_absolute() and path.is_file()),
                        "configured" if path else "missing",
                        f"Set {variable} to an existing absolute certificate path.",
                    )
                )

    return ProductionReport(target=target, checks=tuple(checks))
