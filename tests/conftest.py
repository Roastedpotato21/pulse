from __future__ import annotations

import pytest


class MemoryKeyring:
    """Non-persistent keyring used to keep provider-key tests off the host vault."""

    def __init__(self) -> None:
        self.credentials: dict[tuple[str, str], str] = {}

    def set_password(self, service: str, account: str, value: str) -> None:
        self.credentials[(service, account)] = value

    def get_password(self, service: str, account: str) -> str | None:
        return self.credentials.get((service, account))

    def delete_password(self, service: str, account: str) -> None:
        del self.credentials[(service, account)]


@pytest.fixture
def memory_provider_keyring(monkeypatch: pytest.MonkeyPatch) -> MemoryKeyring:
    from pulse import provider_keys

    backend = MemoryKeyring()
    monkeypatch.setattr(provider_keys, "keyring", backend)
    return backend


@pytest.fixture(autouse=True)
def memory_auth_keyring(monkeypatch: pytest.MonkeyPatch) -> MemoryKeyring:
    """Never let authentication tests touch the developer's real credential vault."""
    from pulse import auth

    backend = MemoryKeyring()
    monkeypatch.setattr(auth, "keyring", backend)
    return backend
