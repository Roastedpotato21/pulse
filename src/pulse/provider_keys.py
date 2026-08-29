"""Safe workspace provider-key management for the Pulse CLI."""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from pulse.providers.manager import PROVIDER_SPECS


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

    Keys are written only to the workspace ``.env`` file, which Pulse excludes
    from Git and release artifacts. Updates are atomic and the file is given
    owner-only permissions where the platform supports POSIX modes.
    """

    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace.resolve()
        self.env_path = self.workspace / ".env"

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
            if self._usable(workspace_value):
                configured, source = True, "workspace .env"
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

    def set(self, provider: str, value: str) -> str:
        spec = self._spec(provider)
        normalized = value.strip()
        if not self._usable(normalized) or "\n" in value or "\r" in value:
            raise ProviderKeyError("API key must be a non-placeholder, single-line value.")
        self._rewrite(spec.env_var, normalized)
        os.environ[spec.env_var] = normalized
        return spec.env_var

    def remove(self, provider: str) -> tuple[str, bool, bool]:
        spec = self._spec(provider)
        values = self._workspace_values()
        removed = spec.env_var in values
        if removed:
            self._rewrite(spec.env_var, None)
        environment_still_set = self._usable(os.environ.get(spec.env_var, ""))
        return spec.env_var, removed, environment_still_set

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
    def _usable(value: str) -> bool:
        normalized = value.strip()
        return bool(normalized and normalized.lower() not in {"replace_me", "placeholder"})
