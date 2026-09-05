"""Fail closed when a release build has no product Google OAuth identity."""

from __future__ import annotations

import json
import re
import urllib.parse
from pathlib import Path
from typing import Any

from hatchling.builders.hooks.plugin.interface import BuildHookInterface

PRODUCT_OAUTH_RESOURCE = Path("src/pulse/_product_oauth.json")
GOOGLE_CLIENT_ID_PATTERN = re.compile(
    r"^[0-9]+-[A-Za-z0-9_-]+\.apps\.googleusercontent\.com$"
)
GOOGLE_CLIENT_SECRET_PATTERN = re.compile(r"^GOCSPX-[A-Za-z0-9_-]{20,}$")


def validate_product_oauth_resource(root: Path) -> dict[str, str]:
    """Validate the exact OAuth resource that Hatch is about to package."""
    resource = root / PRODUCT_OAUTH_RESOURCE
    try:
        raw = resource.read_text(encoding="utf-8")
        config = json.loads(raw)
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise RuntimeError(
            "Release build blocked: configure the product Google Desktop OAuth client first."
        ) from error

    if not isinstance(config, dict) or set(config) != {
        "client_id",
        "client_secret",
        "redirect_uri",
    }:
        raise RuntimeError(
            "Release build blocked: the product OAuth resource has invalid fields."
        )

    client_id = config.get("client_id")
    client_secret = config.get("client_secret")
    redirect_uri = config.get("redirect_uri")
    if not isinstance(client_id, str) or not GOOGLE_CLIENT_ID_PATTERN.fullmatch(client_id):
        raise RuntimeError(
            "Release build blocked: run scripts/configure_product_oauth.py with a valid "
            "PULSE_GOOGLE_CLIENT_ID before building."
        )
    if not isinstance(client_secret, str) or not GOOGLE_CLIENT_SECRET_PATTERN.fullmatch(
        client_secret
    ):
        raise RuntimeError(
            "Release build blocked: run scripts/configure_product_oauth.py with a valid "
            "PULSE_GOOGLE_CLIENT_SECRET before building."
        )
    if not isinstance(redirect_uri, str):
        raise TypeError(
            "Release build blocked: the product OAuth redirect URI must be a string."
        )

    try:
        parsed = urllib.parse.urlparse(redirect_uri)
        port = parsed.port
    except ValueError as error:
        raise RuntimeError(
            "Release build blocked: the product OAuth redirect URI is invalid."
        ) from error
    if (
        parsed.scheme != "http"
        or parsed.hostname != "127.0.0.1"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or (port is not None and not 1 <= port <= 65535)
    ):
        raise RuntimeError(
            "Release build blocked: OAuth must use a plain HTTP 127.0.0.1 loopback URI."
        )

    return {
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri,
    }


class CustomBuildHook(BuildHookInterface):
    """Prevent Hatch from creating an installable artifact with a placeholder client."""

    def initialize(self, version: str, build_data: dict[str, Any]) -> None:
        del build_data
        if self.target_name == "wheel" and version == "editable":
            return
        validate_product_oauth_resource(Path(self.root))
