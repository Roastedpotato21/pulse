"""Secure provider-key management for the Pulse CLI."""

from __future__ import annotations

import hashlib
import hmac
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from pulse.providers.manager import PROVIDER_SPECS

try:
    import keyring
except ImportError:  # pragma: no cover - the release dependency is mandatory
    keyring = None  # type: ignore[assignment]


KEYRING_SERVICE_NAME = "pulse-coding-agent.provider-keys"


class ProviderKeyError(ValueError):
    """Raised when a provider key cannot be safely updated."""


@dataclass(frozen=True, slots=True)
class ProviderKeyStatus:
    provider: str
    environment_variable: str
    configured: bool
    source: str


class ProviderKeyStore:
    """Manage provider keys without accepting secrets as command arguments.

    New keys are stored in the native OS credential vault and scoped to the
    current workspace. Existing environment and ``.env`` credentials remain
    readable for compatibility, but a set/rotation migrates that provider away
    from plaintext workspace storage.
    """

    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace.resolve()
        self.env_path = self.workspace / ".env"
        workspace_digest = hashlib.sha256(
            os.path.normcase(str(self.workspace)).encode("utf-8")
        ).hexdigest()[:24]
        self._account_prefix = f"workspace:{workspace_digest}"

    @staticmethod
    def _spec(provider: str):
        normalized = provider.strip().lower()
        try:
            return PROVIDER_SPECS[normalized]
        except KeyError as error:
            supported = ", ".join(PROVIDER_SPECS)
            raise ProviderKeyError(
                f"Unsupported provider '{provider}'. Choose one of: {supported}."
            ) from error

    def statuses(self) -> tuple[ProviderKeyStatus, ...]:
        workspace_values = self._workspace_values()
        result: list[ProviderKeyStatus] = []
        for spec in PROVIDER_SPECS.values():
            workspace_value = workspace_values.get(spec.env_var, "").strip()
            environment_value = os.environ.get(spec.env_var, "").strip()
            vault_value = self._vault_get(spec.key)
            if self._usable(vault_value):
                configured, source = True, "OS credential vault"
            elif self._usable(workspace_value):
                configured, source = True, "legacy workspace .env"
            elif self._usable(environment_value):
                configured, source = True, "environment"
            else:
                configured, source = False, "not configured"
            result.append(
                ProviderKeyStatus(
                    provider=spec.key,
                    environment_variable=spec.env_var,
                    configured=configured,
                    source=source,
                )
            )
        return tuple(result)

    def get(self, provider: str) -> str | None:
        """Resolve a provider secret without exposing it through status APIs."""
        spec = self._spec(provider)
        vault_value = self._vault_get(spec.key)
        if self._usable(vault_value):
            return vault_value
        environment_value = os.environ.get(spec.env_var, "")
        if self._usable(environment_value):
            return environment_value.strip()
        workspace_value = self._workspace_values().get(spec.env_var, "")
        return workspace_value.strip() if self._usable(workspace_value) else None

    def set(self, provider: str, value: str) -> str:
        spec = self._spec(provider)
        legacy_value = self._workspace_values().get(spec.env_var)
        previous_vault_value = self._vault_get(spec.key)
        normalized = value.strip()
        if not self._usable(normalized) or "\n" in value or "\r" in value:
            raise ProviderKeyError("API key must be a non-placeholder, single-line value.")
        if keyring is None:
            raise ProviderKeyError(
                "The OS credential vault is unavailable; the API key was not stored."
            )
        try:
            keyring.set_password(
                KEYRING_SERVICE_NAME,
                self._account(spec.key),
                normalized,
            )
            persisted = keyring.get_password(
                KEYRING_SERVICE_NAME,
                self._account(spec.key),
            )
        except Exception as error:
            raise ProviderKeyError(
                "The OS credential vault rejected the API key; nothing was stored. "
                "Check the native keyring configuration and try again."
            ) from error
        if not persisted or not hmac.compare_digest(persisted, normalized):
            raise ProviderKeyError(
                "The OS credential vault did not confirm persistence; the API key "
                "was not accepted."
            )

        # Successful set/rotation is also an explicit migration away from the
        # legacy plaintext workspace entry. Never remove it before persistence
        # has been verified.
        if legacy_value is not None:
            try:
                self._rewrite(spec.env_var, None)
            except ProviderKeyError as migration_error:
                try:
                    if previous_vault_value is None:
                        keyring.delete_password(
                            KEYRING_SERVICE_NAME,
                            self._account(spec.key),
                        )
                    else:
                        keyring.set_password(
                            KEYRING_SERVICE_NAME,
                            self._account(spec.key),
                            previous_vault_value,
                        )
                except Exception as rollback_error:
                    raise ProviderKeyError(
                        "The key reached the OS credential vault, but legacy .env "
                        "cleanup and vault rollback both failed. The secret was not "
                        "displayed; resolve storage permissions before retrying."
                    ) from rollback_error
                raise ProviderKeyError(
                    "Legacy .env cleanup failed, so the credential-vault update was "
                    "rolled back. Fix workspace permissions and retry."
                ) from migration_error
            if os.environ.get(spec.env_var) == legacy_value:
                os.environ.pop(spec.env_var, None)
        return spec.env_var

    def rotate(self, provider: str, value: str) -> str:
        """Atomically replace a managed provider secret in the credential vault."""
        return self.set(provider, value)

    def remove(self, provider: str) -> tuple[str, bool, bool]:
        spec = self._spec(provider)
        values = self._workspace_values()
        removed_from_env = spec.env_var in values
        if removed_from_env:
            self._rewrite(spec.env_var, None)
        removed_from_vault = self._vault_delete(spec.key)
        environment_still_set = self._usable(os.environ.get(spec.env_var, ""))
        return spec.env_var, removed_from_env or removed_from_vault, environment_still_set

    def _account(self, provider: str) -> str:
        return f"{self._account_prefix}:{provider}"

    def _vault_get(self, provider: str) -> str | None:
        if keyring is None:
            return None
        try:
            value = keyring.get_password(
                KEYRING_SERVICE_NAME,
                self._account(provider),
            )
        except Exception:  # noqa: BLE001 - unavailable/locked vault means no key
            return None
        return value.strip() if self._usable(value or "") else None

    def _vault_delete(self, provider: str) -> bool:
        if keyring is None or self._vault_get(provider) is None:
            return False
        try:
            keyring.delete_password(
                KEYRING_SERVICE_NAME,
                self._account(provider),
            )
        except Exception as error:
            raise ProviderKeyError(
                "The OS credential vault could not remove the provider key."
            ) from error
        return True

    def _workspace_values(self) -> dict[str, str]:
        values: dict[str, str] = {}
        for line in self._read_lines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, value = stripped.split("=", 1)
            values[key.strip()] = value.strip().strip("\"'")
        return values

    def _read_lines(self) -> list[str]:
        if not self.env_path.exists():
            return []
        if self.env_path.is_symlink():
            raise ProviderKeyError("Refusing to manage a symbolic-link .env file.")
        try:
            self.env_path.resolve().relative_to(self.workspace)
        except ValueError as error:
            raise ProviderKeyError("The workspace .env file escapes the project root.") from error
        return self.env_path.read_text(encoding="utf-8").splitlines()

    def _rewrite(self, variable: str, value: str | None) -> None:
        lines = self._read_lines()
        updated: list[str] = []
        replaced = False
        for line in lines:
            stripped = line.strip()
            if stripped and not stripped.startswith("#") and "=" in stripped:
                key = stripped.split("=", 1)[0].strip()
                if key == variable:
                    if value is not None and not replaced:
                        updated.append(f"{variable}={value}")
                        replaced = True
                    continue
            updated.append(line)
        if value is not None and not replaced:
            if updated and updated[-1]:
                updated.append("")
            updated.append(f"{variable}={value}")

        self.workspace.mkdir(parents=True, exist_ok=True)
        temporary_name: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                newline="\n",
                prefix=".pulse-env-",
                dir=self.workspace,
                delete=False,
            ) as temporary:
                temporary_name = temporary.name
                temporary.write("\n".join(updated) + ("\n" if updated else ""))
            os.chmod(temporary_name, 0o600)
            os.replace(temporary_name, self.env_path)
            temporary_name = None
        except OSError as error:
            raise ProviderKeyError(f"Unable to update {self.env_path}: {error}") from error
        finally:
            if temporary_name:
                try:
                    Path(temporary_name).unlink()
                except OSError:
                    pass

    @staticmethod
    def _usable(value: str | None) -> bool:
        if value is None:
            return False
        normalized = value.strip()
        return bool(normalized and normalized.lower() not in {"replace_me", "placeholder"})
