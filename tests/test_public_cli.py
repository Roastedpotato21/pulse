from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from pulse.provider_keys import ProviderKeyError, ProviderKeyStore


def _module_env() -> dict[str, str]:
    env = os.environ.copy()
    src_path = Path(__file__).parent.parent / "src"
    env["PYTHONPATH"] = str(src_path) + os.pathsep + env.get("PYTHONPATH", "")
    return env


def test_public_help_has_release_auth_and_key_commands() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "pulse.cli", "--help"],
        check=True,
        capture_output=True,
        text=True,
        env=_module_env(),
    )

    assert "version" in result.stdout
    assert "keys" in result.stdout
    assert "login" in result.stdout
    assert "logout" in result.stdout
    assert "register" not in result.stdout
    assert "google-login" not in result.stdout


def test_version_subcommand_matches_version_flag() -> None:
    command = [sys.executable, "-m", "pulse.cli"]
    version = subprocess.run(
        [*command, "version"], check=True, capture_output=True, text=True, env=_module_env()
    )
    flag = subprocess.run(
        [*command, "--version"], check=True, capture_output=True, text=True, env=_module_env()
    )

    assert version.stdout.strip() == flag.stdout.strip() == "pulse 0.1.0"


def test_provider_key_rotation_never_returns_or_duplicates_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = "synthetic-provider-key-value"
    env_file = tmp_path / ".env"
    env_file.write_text("KEEP_ME=yes\nOPENAI_API_KEY=old-value\n", encoding="utf-8")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    store = ProviderKeyStore(tmp_path)

    assert store.set("openai", secret) == "OPENAI_API_KEY"
    content = env_file.read_text(encoding="utf-8")
    assert content.count("OPENAI_API_KEY=") == 1
    assert "KEEP_ME=yes" in content
    assert secret in content
    status = next(item for item in store.statuses() if item.provider == "openai")
    assert status.configured and status.source == "workspace .env"
    assert secret not in repr(status)

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    variable, removed, environment_still_set = store.remove("openai")
    assert variable == "OPENAI_API_KEY"
    assert removed and not environment_still_set
    assert secret not in env_file.read_text(encoding="utf-8")


def test_provider_key_rejects_unsafe_values(tmp_path: Path) -> None:
    store = ProviderKeyStore(tmp_path)
    with pytest.raises(ProviderKeyError, match="single-line"):
        store.set("openai", "first\nsecond")
    with pytest.raises(ProviderKeyError, match="Unsupported provider"):
        store.set("unknown", "secret")


def test_keys_set_cli_uses_hidden_prompt_and_does_not_echo_secret(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from pulse import cli

    secret = "synthetic-cli-key-value"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["pulse", "keys", "set", "openai"])
    monkeypatch.setattr(cli.getpass, "getpass", lambda _prompt: secret)

    cli.main()

    output = capsys.readouterr().out
    assert "Updated OPENAI_API_KEY" in output
    assert secret not in output
    assert secret in (tmp_path / ".env").read_text(encoding="utf-8")


def test_login_is_routed_without_building_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    from pulse import cli

    called = False

    def fake_login() -> None:
        nonlocal called
        called = True

    monkeypatch.setattr(cli, "_handle_login_command", fake_login)
    monkeypatch.setattr(
        cli,
        "build_runtime",
        lambda *_args, **_kwargs: pytest.fail("login must not build the agent runtime"),
    )

    monkeypatch.setattr(sys, "argv", ["pulse", "login"])
    cli.main()
    assert called


def test_logout_clears_auth_without_building_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pulse import cli

    class FakeAuth:
        def __init__(self) -> None:
            self.logged_out = False

        def logout(self) -> bool:
            self.logged_out = True
            return True

    fake_auth = FakeAuth()
    monkeypatch.setattr(cli, "AuthenticationManager", lambda *_args: fake_auth)
    monkeypatch.setattr(
        cli,
        "build_runtime",
        lambda *_args, **_kwargs: pytest.fail("logout must not build the agent runtime"),
    )

    monkeypatch.setattr(sys, "argv", ["pulse", "logout"])
    cli.main()
    assert fake_auth.logged_out
