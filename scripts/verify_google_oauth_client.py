"""Check that Google's token endpoint recognizes the release as a public client."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request

try:
    from scripts.configure_product_oauth import (
        CLIENT_ID_ENV,
        CLIENT_ID_PATTERN,
        CLIENT_SECRET_ENV,
        CLIENT_SECRET_PATTERN,
        DEFAULT_REDIRECT_URI,
    )
except ImportError:  # Direct execution places scripts/ rather than the repository on sys.path.
    from configure_product_oauth import (  # type: ignore[no-redef]
        CLIENT_ID_ENV,
        CLIENT_ID_PATTERN,
        CLIENT_SECRET_ENV,
        CLIENT_SECRET_PATTERN,
        DEFAULT_REDIRECT_URI,
    )


def verify() -> None:
    client_id = os.environ.get(CLIENT_ID_ENV, "").strip()
    if not CLIENT_ID_PATTERN.fullmatch(client_id):
        raise ValueError("The release Google Desktop OAuth client ID is missing or malformed.")
    client_secret = os.environ.get(CLIENT_SECRET_ENV, "").strip()
    if not CLIENT_SECRET_PATTERN.fullmatch(client_secret):
        raise ValueError(
            "The release Google Desktop OAuth client credential is missing or malformed."
        )
    request = urllib.request.Request(
        "https://oauth2.googleapis.com/token",
        data=urllib.parse.urlencode(
            {
                "client_id": client_id,
                "client_secret": client_secret,
                "code": "pulse-release-invalid-code",
                "code_verifier": "a" * 64,
                "grant_type": "authorization_code",
                "redirect_uri": DEFAULT_REDIRECT_URI,
            }
        ).encode("utf-8"),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        urllib.request.urlopen(request, timeout=15)
    except urllib.error.HTTPError as error:
        try:
            payload = json.loads(error.read().decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as parse_error:
            raise RuntimeError("Google returned an unreadable OAuth validation response.") from parse_error
        oauth_error = payload.get("error")
        if oauth_error not in {"invalid_grant", "invalid_request"}:
            raise RuntimeError(
                f"Google rejected the configured product OAuth client ({oauth_error or 'unknown'})."
            ) from error
        return
    except urllib.error.URLError as error:
        raise RuntimeError("Google OAuth client validation could not reach Google.") from error
    raise RuntimeError("Google unexpectedly accepted the deliberately invalid release test code.")


def main() -> None:
    verify()
    print("Google recognized the release OAuth client credentials.")


if __name__ == "__main__":
    main()
