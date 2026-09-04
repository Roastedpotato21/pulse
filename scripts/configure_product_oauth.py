"""Embed Pulse's public installed-app OAuth identity in a release build."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from urllib.parse import urlparse

CLIENT_ID_ENV = "PULSE_GOOGLE_CLIENT_ID"
REDIRECT_URI_ENV = "PULSE_GOOGLE_REDIRECT_URI"
DEFAULT_REDIRECT_URI = "http://127.0.0.1"
OUTPUT = Path(__file__).parent.parent / "src" / "pulse" / "_product_oauth.json"
CLIENT_ID_PATTERN = re.compile(
    r"^[0-9]+-[A-Za-z0-9_-]+\.apps\.googleusercontent\.com$"
)


def _validate_redirect_uri(value: str) -> str:
    try:
        parsed = urlparse(value)
        port = parsed.port
    except ValueError as error:
        raise ValueError("PULSE_GOOGLE_REDIRECT_URI is not a valid URL.") from error
    if (
        parsed.scheme != "http"
        or parsed.hostname != "127.0.0.1"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or (port is not None and not 1 <= port <= 65535)
    ):
        raise ValueError(
            "PULSE_GOOGLE_REDIRECT_URI must be a plain HTTP 127.0.0.1 loopback URL."
        )
    return value


def configure() -> Path:
    client_id = os.environ.get(CLIENT_ID_ENV, "").strip()
    if not CLIENT_ID_PATTERN.fullmatch(client_id):
        raise ValueError(
            "PULSE_GOOGLE_CLIENT_ID must contain the release's Google Desktop client ID."
        )
    redirect_uri = _validate_redirect_uri(
        os.environ.get(REDIRECT_URI_ENV, DEFAULT_REDIRECT_URI).strip()
        or DEFAULT_REDIRECT_URI
    )
    OUTPUT.write_text(
        json.dumps(
            {"client_id": client_id, "redirect_uri": redirect_uri},
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return OUTPUT


def main() -> None:
    configure()
    print("Configured the public Google Desktop OAuth client for this product build.")


if __name__ == "__main__":
    main()
