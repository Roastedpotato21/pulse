from __future__ import annotations

import asyncio
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest

from pulse.config import (
    AgentConfig,
    LoggingConfig,
    ModelConfig,
    SandboxConfig,
)
from pulse.production import is_secure_remote_token, run_production_checks
from pulse.sandbox.remote.server import RemoteExecutionStore, RemoteServer

STRONG_TOKEN = "A7mQ2xL9vR4pN8cK6sJ3wH5yB1dF0gZ9uT"
SYNTHETIC_GOOGLE_CLIENT_SECRET = "GOCSPX-abcdefghijklmnopqrstuvwxyz123456"


def test_pinned_gitleaks_action_uses_compatible_global_allowlist() -> None:
    config = tomllib.loads(Path(".gitleaks.toml").read_text(encoding="utf-8"))
    assert "allowlist" in config
    assert "allowlists" not in config
    assert STRONG_TOKEN in config["allowlist"]["regexes"]
    assert SYNTHETIC_GOOGLE_CLIENT_SECRET in config["allowlist"]["regexes"]


def _config(workspace: Path) -> AgentConfig:
    return AgentConfig(
        agent_name="Pulse",
        mode="single-model",
        model=ModelConfig("openrouter", "model", 0.2),
        sandbox=SandboxConfig(workspace, True, True, False),
        logging=LoggingConfig(
            workspace / ".agent" / "logs" / "actions.jsonl",
            workspace / ".agent" / "logs" / "telemetry.jsonl",
        ),
    )


def test_local_production_report_allows_missing_optional_container_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "agent.config.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        "pulse.production.shutil.which",
        lambda command: "/bin/pulse" if command == "pulse" else None,
    )

    report = run_production_checks(
        tmp_path,
        _config(tmp_path),
        provider_configured=True,
        environ={},
    )

    assert report.passed
    container = next(check for check in report.checks if check.name == "container_runtime")
    assert not container.ok and not container.blocking


def test_remote_production_report_rejects_placeholder_and_relative_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "agent.config.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr("pulse.production.shutil.which", lambda _command: None)
    report = run_production_checks(
        tmp_path,
        _config(tmp_path),
        provider_configured=True,
        target="remote",
        environ={
            "PULSE_REMOTE_TOKEN": "replace_with_a_random_32_character_token",
            "PULSE_REMOTE_WORKSPACE_ROOT": "relative/workspaces",
            "PULSE_REMOTE_DB": "relative/executions.sqlite3",
        },
    )

    assert not report.passed
    failed = {check.name for check in report.checks if check.blocking and not check.ok}
    assert {"remote_tokens", "remote_workspace_root", "remote_database"} <= failed


def test_remote_production_report_accepts_hardened_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "agent.config.json").write_text("{}", encoding="utf-8")
    certs = []
    for name in ("server.crt", "server.key", "ca.crt"):
        path = tmp_path / name
        path.write_text("fixture", encoding="utf-8")
        certs.append(path)
    monkeypatch.setattr(
        "pulse.production.shutil.which",
        lambda command: f"/bin/{command}" if command in {"pulse", "docker"} else None,
    )

    report = run_production_checks(
        tmp_path,
        _config(tmp_path),
        provider_configured=True,
        target="remote",
        environ={
            "PULSE_REMOTE_HOST": "worker.internal",
            "PULSE_REMOTE_TOKEN": STRONG_TOKEN,
            "PULSE_REMOTE_WORKSPACE_ROOT": str((tmp_path / "workspaces").resolve()),
            "PULSE_REMOTE_DB": str((tmp_path / "executions.sqlite3").resolve()),
            "PULSE_TLS_CERT": str(certs[0].resolve()),
            "PULSE_TLS_KEY": str(certs[1].resolve()),
            "PULSE_TLS_CA": str(certs[2].resolve()),
        },
    )

    assert report.passed


def test_secure_remote_token_policy_rejects_predictable_values() -> None:
    assert is_secure_remote_token(STRONG_TOKEN)
    assert not is_secure_remote_token("short")
    assert not is_secure_remote_token("replace_with_a_random_32_character_token")
    assert not is_secure_remote_token("a" * 64)


def test_remote_server_production_mode_rejects_weak_tokens(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="random, non-placeholder"):
        RemoteServer(
            auth_token="test-token",
            workspace_root=tmp_path / "workspaces",
            database_path=tmp_path / "executions.sqlite3",
            production_mode=True,
        )


def test_remote_store_persists_correlation_id(tmp_path: Path) -> None:
    store = RemoteExecutionStore(tmp_path / "executions.sqlite3")
    store.create("execution-1", "tenant-1", correlation_id="request-123")

    assert store.get("execution-1", "tenant-1")["correlation_id"] == "request-123"


def test_remote_health_and_readiness_endpoints(tmp_path: Path) -> None:
    server = RemoteServer(
        auth_token="development-token",
        workspace_root=tmp_path / "workspaces",
        database_path=tmp_path / "executions.sqlite3",
    )
    health = asyncio.run(
        server._process_request(
            None, SimpleNamespace(path="/healthz", headers={})
        )
    )
    not_ready = asyncio.run(
        server._process_request(
            None, SimpleNamespace(path="/readyz", headers={})
        )
    )
    server._ready = True
    ready = asyncio.run(
        server._process_request(
            None, SimpleNamespace(path="/readyz", headers={})
        )
    )

    assert health.status_code == 200
    assert not_ready.status_code == 503
    assert ready.status_code == 200
