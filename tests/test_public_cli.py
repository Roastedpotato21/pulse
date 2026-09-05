from __future__ import annotations

import os
import subprocess
import sys
import warnings
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

    assert version.stdout.strip() == flag.stdout.strip() == "pulse 0.1.2"


def test_provider_key_rotation_never_returns_or_duplicates_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, memory_provider_keyring
) -> None:
    secret = "synthetic-provider-key-value"
    env_file = tmp_path / ".env"
    env_file.write_text("KEEP_ME=yes\nOPENAI_API_KEY=old-value\n", encoding="utf-8")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    store = ProviderKeyStore(tmp_path)

    assert store.set("openai", secret) == "OPENAI_API_KEY"
    content = env_file.read_text(encoding="utf-8")
    assert "OPENAI_API_KEY=" not in content
    assert "KEEP_ME=yes" in content
    assert secret not in content
    assert secret in memory_provider_keyring.credentials.values()
    status = next(item for item in store.statuses() if item.provider == "openai")
    assert status.configured and status.source == "OS credential vault"
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


def test_provider_key_store_fails_closed_and_preserves_legacy_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pulse import provider_keys

    class FailingKeyring:
        @staticmethod
        def get_password(_service: str, _account: str) -> None:
            return None

        @staticmethod
        def set_password(_service: str, _account: str, _value: str) -> None:
            raise RuntimeError("vault unavailable")

    env_file = tmp_path / ".env"
    env_file.write_text("OPENAI_API_KEY=legacy-value\n", encoding="utf-8")
    monkeypatch.setattr(provider_keys, "keyring", FailingKeyring())

    with pytest.raises(ProviderKeyError, match="credential vault rejected"):
        ProviderKeyStore(tmp_path).set("openai", "replacement-value")

    assert env_file.read_text(encoding="utf-8") == "OPENAI_API_KEY=legacy-value\n"


def test_provider_key_migration_rolls_back_vault_if_plaintext_cleanup_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    memory_provider_keyring,
) -> None:
    store = ProviderKeyStore(tmp_path)
    store.set("openai", "previous-vault-value")
    env_file = tmp_path / ".env"
    env_file.write_text("OPENAI_API_KEY=legacy-value\n", encoding="utf-8")

    def fail_cleanup(_variable: str, _value: str | None) -> None:
        raise ProviderKeyError("synthetic cleanup failure")

    monkeypatch.setattr(store, "_rewrite", fail_cleanup)

    with pytest.raises(ProviderKeyError, match="vault update was rolled back"):
        store.rotate("openai", "replacement-value")

    assert store.get("openai") == "previous-vault-value"
    assert env_file.read_text(encoding="utf-8") == "OPENAI_API_KEY=legacy-value\n"


def test_keys_set_cli_uses_hidden_prompt_and_does_not_echo_secret(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    memory_provider_keyring,
) -> None:
    from pulse import cli

    secret = "synthetic-cli-key-value"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["pulse", "keys", "set", "openai"])
    monkeypatch.setattr(cli.getpass, "getpass", lambda _prompt: secret)

    cli.main()

    output = capsys.readouterr().out
    assert "Stored OPENAI_API_KEY in the OS credential vault" in output
    assert secret not in output
    assert not (tmp_path / ".env").exists()
    assert secret in memory_provider_keyring.credentials.values()


def test_hidden_key_input_fails_closed_when_terminal_would_echo(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from pulse import cli

    secret = "must-not-be-read"

    def insecure_getpass(_prompt: str) -> str:
        warnings.warn("echo unavailable", cli.getpass.GetPassWarning)
        return secret

    monkeypatch.setattr(cli.getpass, "getpass", insecure_getpass)

    assert cli._read_hidden_provider_key("openai") is None
    assert secret not in capsys.readouterr().out


def test_successful_login_onboards_provider_model_and_hidden_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    memory_provider_keyring,
) -> None:
    from pulse import cli
    from pulse.auth import UserProfile

    secret = "synthetic-onboarding-key"
    answers = iter(["3"])
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "is_authenticated", lambda: False)
    monkeypatch.setattr(
        cli,
        "login",
        lambda: UserProfile(email="user@example.com", name="User", sub="subject"),
    )
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))
    monkeypatch.setattr(cli.getpass, "getpass", lambda _prompt: secret)
    monkeypatch.setattr(
        cli.ProviderManager,
        "resolve_auto_model",
        lambda self, provider, api_key, persist=False: "gpt-5.6-terra",
    )
    monkeypatch.setattr(sys, "argv", ["pulse", "login"])

    cli.main()

    selection = (tmp_path / ".agent" / "provider.json").read_text(encoding="utf-8")
    output = capsys.readouterr().out
    assert '"provider": "openai"' in selection
    assert '"model": "gpt-5.6-terra"' in selection
    assert '"selection_mode": "auto"' in selection
    assert "BYOK setup complete" in output
    assert secret not in output
    assert secret in memory_provider_keyring.credentials.values()
    assert not (tmp_path / ".env").exists()


def test_login_configuration_error_never_instructs_user_to_create_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from pulse import cli
    from pulse.auth import AuthConfigurationError

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "is_authenticated", lambda: False)
    monkeypatch.setattr(
        cli,
        "login",
        lambda: (_ for _ in ()).throw(AuthConfigurationError("missing")),
    )

    with pytest.raises(SystemExit):
        cli._handle_login_command()

    output = capsys.readouterr().out
    assert "latest official pulse-coding-agent release" in output
    assert "Set GOOGLE_CLIENT" not in output
    assert "create a .env" not in output
    assert not (tmp_path / ".env").exists()


def test_keys_without_subcommand_opens_manager_and_rotates_without_disclosure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    memory_provider_keyring,
) -> None:
    from pulse import cli

    old_secret = "synthetic-old-key"
    new_secret = "synthetic-rotated-key"
    ProviderKeyStore(tmp_path).set("openai", old_secret)
    answers = iter(["openai", "rotate", ""])
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))
    monkeypatch.setattr(cli.getpass, "getpass", lambda _prompt: new_secret)
    monkeypatch.setattr(sys, "argv", ["pulse", "keys"])

    cli.main()

    output = capsys.readouterr().out
    assert "Rotated OPENAI_API_KEY in the OS credential vault" in output
    assert old_secret not in output
    assert new_secret not in output
    assert old_secret not in memory_provider_keyring.credentials.values()
    assert new_secret in memory_provider_keyring.credentials.values()


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
