from __future__ import annotations

import json

import pytest

from scripts import configure_product_oauth


def test_product_oauth_build_configuration_requires_desktop_client(monkeypatch, tmp_path):
    monkeypatch.setattr(configure_product_oauth, "OUTPUT", tmp_path / "oauth.json")
    monkeypatch.setenv("PULSE_GOOGLE_CLIENT_ID", "replace_me")

    with pytest.raises(ValueError, match="Google Desktop client ID"):
        configure_product_oauth.configure()


def test_product_oauth_build_configuration_contains_no_secret(monkeypatch, tmp_path):
    output = tmp_path / "oauth.json"
    monkeypatch.setattr(configure_product_oauth, "OUTPUT", output)
    monkeypatch.setenv(
        "PULSE_GOOGLE_CLIENT_ID",
        "123456789012-product.apps.googleusercontent.com",
    )
    monkeypatch.delenv("PULSE_GOOGLE_REDIRECT_URI", raising=False)

    assert configure_product_oauth.configure() == output
    config = json.loads(output.read_text(encoding="utf-8"))
    assert config == {
        "client_id": "123456789012-product.apps.googleusercontent.com",
        "redirect_uri": "http://127.0.0.1",
    }
    assert "secret" not in output.read_text(encoding="utf-8").lower()
