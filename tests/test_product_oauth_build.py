from __future__ import annotations

import json

import pytest

from hatch_build import validate_product_oauth_resource
from scripts import configure_product_oauth


def test_product_oauth_build_configuration_requires_desktop_client(monkeypatch, tmp_path):
    monkeypatch.setattr(configure_product_oauth, "OUTPUT", tmp_path / "oauth.json")
    monkeypatch.setenv("PULSE_GOOGLE_CLIENT_ID", "replace_me")
    monkeypatch.setenv(
        "PULSE_GOOGLE_CLIENT_SECRET", "GOCSPX-abcdefghijklmnopqrstuvwxyz123456"
    )

    with pytest.raises(ValueError, match="Google Desktop client ID"):
        configure_product_oauth.configure()


def test_product_oauth_build_configuration_requires_desktop_credential(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(configure_product_oauth, "OUTPUT", tmp_path / "oauth.json")
    monkeypatch.setenv(
        "PULSE_GOOGLE_CLIENT_ID",
        "123456789012-product.apps.googleusercontent.com",
    )
    monkeypatch.delenv("PULSE_GOOGLE_CLIENT_SECRET", raising=False)

    with pytest.raises(ValueError, match="PULSE_GOOGLE_CLIENT_SECRET"):
        configure_product_oauth.configure()


def test_product_oauth_build_configuration_contains_desktop_credential(
    monkeypatch, tmp_path
):
    output = tmp_path / "oauth.json"
    monkeypatch.setattr(configure_product_oauth, "OUTPUT", output)
    monkeypatch.setenv(
        "PULSE_GOOGLE_CLIENT_ID",
        "123456789012-product.apps.googleusercontent.com",
    )
    monkeypatch.setenv(
        "PULSE_GOOGLE_CLIENT_SECRET",
        "GOCSPX-abcdefghijklmnopqrstuvwxyz123456",
    )
    monkeypatch.delenv("PULSE_GOOGLE_REDIRECT_URI", raising=False)

    assert configure_product_oauth.configure() == output
    config = json.loads(output.read_text(encoding="utf-8"))
    assert config == {
        "client_id": "123456789012-product.apps.googleusercontent.com",
        "client_secret": "GOCSPX-abcdefghijklmnopqrstuvwxyz123456",
        "redirect_uri": "http://127.0.0.1",
    }


def test_hatch_build_blocks_placeholder_oauth_resource(tmp_path):
    resource = tmp_path / "src" / "pulse" / "_product_oauth.json"
    resource.parent.mkdir(parents=True)
    resource.write_text(
        json.dumps(
            {
                "client_id": "not-configured",
                "client_secret": "not-configured",
                "redirect_uri": "http://127.0.0.1",
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="Release build blocked"):
        validate_product_oauth_resource(tmp_path)


def test_hatch_build_accepts_configured_public_client(tmp_path):
    resource = tmp_path / "src" / "pulse" / "_product_oauth.json"
    resource.parent.mkdir(parents=True)
    resource.write_text(
        json.dumps(
            {
                "client_id": "123456789012-product.apps.googleusercontent.com",
                "client_secret": "GOCSPX-abcdefghijklmnopqrstuvwxyz123456",
                "redirect_uri": "http://127.0.0.1",
            }
        ),
        encoding="utf-8",
    )

    assert validate_product_oauth_resource(tmp_path) == {
        "client_id": "123456789012-product.apps.googleusercontent.com",
        "client_secret": "GOCSPX-abcdefghijklmnopqrstuvwxyz123456",
        "redirect_uri": "http://127.0.0.1",
    }
