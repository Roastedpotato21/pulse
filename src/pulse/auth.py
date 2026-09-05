"""Production-Grade Authentication Management for Pulse CLI.

Implements OAuth 2.0 Authorization Code Flow with PKCE (Proof Key for Code Exchange),
secure OS credential storage via keyring, automatic silent token refresh, authenticated
Google userinfo lookup, and clean session management APIs for the Pulse developer CLI.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import html as html_lib
import http.server
import importlib.resources
import json
import logging
import os
import re
import secrets
import socketserver
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    import keyring
except ImportError:  # pragma: no cover
    keyring = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

KEYRING_SERVICE_NAME = "pulse-cli"
KEYRING_ACCOUNT_NAME = "current_session"
KEYRING_SESSION_VERSION = 2
KEYRING_CHUNK_CHAR_LIMIT = 1000
KEYRING_MAX_CHUNKS_PER_FIELD = 16
KEYRING_SESSION_FIELDS = ("user", "access_token", "refresh_token", "id_token")
PRODUCT_OAUTH_RESOURCE = "_product_oauth.json"
PRODUCT_CLIENT_ID_ENV = "PULSE_GOOGLE_CLIENT_ID"
PRODUCT_CLIENT_SECRET_ENV = "PULSE_GOOGLE_CLIENT_SECRET"
PRODUCT_REDIRECT_URI_ENV = "PULSE_GOOGLE_REDIRECT_URI"
DEFAULT_REDIRECT_URI = "http://127.0.0.1"
_GOOGLE_CLIENT_ID = re.compile(
    r"^[0-9]+-[A-Za-z0-9_-]+\.apps\.googleusercontent\.com$"
)
_GOOGLE_CLIENT_SECRET = re.compile(r"^GOCSPX-[A-Za-z0-9_-]{20,}$")
_UNCONFIGURED_CLIENT_IDS = {"", "not-configured", "placeholder", "replace_me"}


# --- Exceptions ---

class AuthError(Exception):
    """Base exception for authentication errors."""


class AuthConfigurationError(AuthError):
    """Raised when an installation has no valid product OAuth identity."""


class AuthTimeoutError(AuthError):
    """Raised when authentication times out waiting for browser callback."""


class StateMismatchError(AuthError):
    """Raised when OAuth state token does not match (potential CSRF)."""


class UserCancelledError(AuthError):
    """Raised when user cancels authentication in browser."""


# --- Data Models ---

@dataclass(frozen=True, slots=True)
class UserProfile:
    """User profile data extracted from authenticated ID token / userinfo."""

    email: str
    name: str | None = None
    picture: str | None = None
    sub: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "email": self.email,
            "name": self.name,
            "picture": self.picture,
            "sub": self.sub,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> UserProfile:
        return cls(
            email=data.get("email", ""),
            name=data.get("name"),
            picture=data.get("picture"),
            sub=data.get("sub"),
        )


@dataclass(frozen=True, slots=True)
class TokenSet:
    """OAuth 2.0 Token container."""

    access_token: str
    refresh_token: str | None = None
    id_token: str | None = None
    expires_at: float = 0.0

    @property
    def is_expired(self) -> bool:
        """Token is considered expired 60 seconds before actual expiration."""
        return time.time() >= (self.expires_at - 60)

    def to_dict(self) -> dict[str, Any]:
        return {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "id_token": self.id_token,
            "expires_at": self.expires_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TokenSet:
        return cls(
            access_token=data.get("access_token", ""),
            refresh_token=data.get("refresh_token"),
            id_token=data.get("id_token"),
            expires_at=float(data.get("expires_at", 0.0)),
        )


# --- PKCE & Cryptographic Utilities ---

def generate_pkce_pair() -> tuple[str, str]:
    """Generate PKCE code_verifier and S256 code_challenge.

    Returns:
        Tuple of (code_verifier, code_challenge)
    """
    raw_bytes = secrets.token_bytes(64)
    code_verifier = base64.urlsafe_b64encode(raw_bytes).decode("utf-8").rstrip("=")
    digest = hashlib.sha256(code_verifier.encode("utf-8")).digest()
    code_challenge = base64.urlsafe_b64encode(digest).decode("utf-8").rstrip("=")
    return code_verifier, code_challenge


def generate_state() -> str:
    """Generate a secure cryptographic state token."""
    return secrets.token_urlsafe(32)


def decode_jwt_payload(jwt_token: str) -> dict[str, Any]:
    """Safely decode unverified payload segment of a JWT string.

    Args:
        jwt_token: Compact JWT string (header.payload.signature).

    Returns:
        Decoded payload dictionary.
    """
    parts = jwt_token.split(".")
    if len(parts) < 2:
        return {}
    payload_b64 = parts[1]
    padding = "=" * (-len(payload_b64) % 4)
    decoded_bytes = base64.urlsafe_b64decode((payload_b64 + padding).encode("utf-8"))
    decoded: Any = json.loads(decoded_bytes.decode("utf-8"))
    if not isinstance(decoded, dict):
        return {}
    return {str(key): value for key, value in decoded.items()}


# --- Secure Storage (Keyring + File Fallback) ---

class SecureTokenStore:
    """Workspace-scoped credential storage backed only by the OS keyring."""

    def __init__(self, workspace: Path | None = None) -> None:
        self.workspace = (workspace or Path.cwd()).resolve()
        self.fallback_file = self.workspace / ".agent" / ".pulse-auth-session.json"
        digest = hashlib.sha256(
            os.path.normcase(str(self.workspace)).encode("utf-8")
        ).hexdigest()[:24]
        self.account_name = f"{KEYRING_ACCOUNT_NAME}:{digest}"
        self.oauth_config_account = f"{self.account_name}:oauth-client"

    def _field_account(self, field: str, index: int) -> str:
        return f"{self.account_name}:{field}:{index}"

    def _split_value(self, value: str) -> list[str]:
        chunks = [
            value[index:index + KEYRING_CHUNK_CHAR_LIMIT]
            for index in range(0, len(value), KEYRING_CHUNK_CHAR_LIMIT)
        ]
        if len(chunks) > KEYRING_MAX_CHUNKS_PER_FIELD:
            raise AuthError("The authentication session is too large for secure storage.")
        return chunks

    def _manifest_accounts(self, raw_manifest: str | None) -> set[str]:
        if not raw_manifest:
            return set()
        try:
            manifest = json.loads(raw_manifest)
        except (json.JSONDecodeError, TypeError):
            return set()
        if not isinstance(manifest, dict) or manifest.get("version") != KEYRING_SESSION_VERSION:
            return set()
        counts = manifest.get("chunks")
        if not isinstance(counts, dict):
            return set()

        accounts: set[str] = set()
        for field in KEYRING_SESSION_FIELDS:
            count = counts.get(field, 0)
            if (
                isinstance(count, bool)
                or not isinstance(count, int)
                or not 0 <= count <= KEYRING_MAX_CHUNKS_PER_FIELD
            ):
                return set()
            accounts.update(self._field_account(field, index) for index in range(count))
        return accounts

    def _load_chunked_session(self, manifest: dict[str, Any]) -> tuple[UserProfile, TokenSet] | None:
        counts = manifest.get("chunks")
        if not isinstance(counts, dict) or set(counts) - set(KEYRING_SESSION_FIELDS):
            return None

        values: dict[str, str | None] = {}
        for field in KEYRING_SESSION_FIELDS:
            count = counts.get(field, 0)
            if (
                isinstance(count, bool)
                or not isinstance(count, int)
                or not 0 <= count <= KEYRING_MAX_CHUNKS_PER_FIELD
                or (field in {"user", "access_token"} and count == 0)
            ):
                return None

            chunks: list[str] = []
            for index in range(count):
                chunk = keyring.get_password(
                    KEYRING_SERVICE_NAME,
                    self._field_account(field, index),
                )
                if not isinstance(chunk, str) or not chunk:
                    return None
                chunks.append(chunk)
            values[field] = "".join(chunks) if chunks else None

        raw_user = values["user"]
        if not isinstance(raw_user, str):
            return None
        user_data = json.loads(raw_user)
        if not isinstance(user_data, dict):
            return None
        user = UserProfile.from_dict(user_data)
        access_token = values["access_token"]
        if not user.email or not isinstance(access_token, str) or not access_token:
            return None
        tokens = TokenSet(
            access_token=access_token,
            refresh_token=values["refresh_token"],
            id_token=values["id_token"],
            expires_at=float(manifest.get("expires_at", 0.0)),
        )
        return user, tokens

    def store_session(self, user: UserProfile, tokens: TokenSet) -> None:
        if keyring is None:
            raise AuthError("The OS credential vault is unavailable; the session was not stored.")

        field_values = {
            "user": json.dumps(user.to_dict(), separators=(",", ":")),
            "access_token": tokens.access_token,
            "refresh_token": tokens.refresh_token or "",
            "id_token": tokens.id_token or "",
        }
        field_chunks = {
            field: self._split_value(value) if value else []
            for field, value in field_values.items()
        }
        manifest = json.dumps(
            {
                "version": KEYRING_SESSION_VERSION,
                "expires_at": tokens.expires_at,
                "chunks": {field: len(field_chunks[field]) for field in KEYRING_SESSION_FIELDS},
            },
            separators=(",", ":"),
        )
        new_entries = {
            self._field_account(field, index): chunk
            for field, chunks in field_chunks.items()
            for index, chunk in enumerate(chunks)
        }

        try:
            previous_manifest = keyring.get_password(KEYRING_SERVICE_NAME, self.account_name)
            old_accounts = self._manifest_accounts(previous_manifest)
            touched_accounts = old_accounts | set(new_entries) | {self.account_name}
            previous_values = {
                account: keyring.get_password(KEYRING_SERVICE_NAME, account)
                for account in touched_accounts
            }

            for account, value in new_entries.items():
                keyring.set_password(KEYRING_SERVICE_NAME, account, value)
                persisted = keyring.get_password(KEYRING_SERVICE_NAME, account)
                if not persisted or not hmac.compare_digest(persisted, value):
                    raise RuntimeError("The OS credential vault did not confirm a session chunk.")

            # The manifest is written last and acts as the commit marker.
            keyring.set_password(KEYRING_SERVICE_NAME, self.account_name, manifest)
            persisted_manifest = keyring.get_password(KEYRING_SERVICE_NAME, self.account_name)
            if not persisted_manifest or not hmac.compare_digest(persisted_manifest, manifest):
                raise RuntimeError("The OS credential vault did not confirm the session manifest.")
        except Exception as error:
            for account, previous_value in locals().get("previous_values", {}).items():
                try:
                    if previous_value is None:
                        keyring.delete_password(KEYRING_SERVICE_NAME, account)
                    else:
                        keyring.set_password(KEYRING_SERVICE_NAME, account, previous_value)
                except Exception:  # noqa: BLE001
                    logger.debug("OS credential vault rollback was incomplete.")
            logger.debug("OS credential vault storage failed.")
            raise AuthError(
                "The OS credential vault rejected the session; no plaintext fallback was written."
            ) from error

        for stale_account in old_accounts - set(new_entries):
            try:
                keyring.delete_password(KEYRING_SERVICE_NAME, stale_account)
            except Exception:  # noqa: BLE001
                logger.debug("A stale OS credential vault entry could not be removed.")
        if self.fallback_file.exists():
            try:
                self.fallback_file.unlink()
            except OSError:
                logger.warning("A legacy plaintext authentication file could not be removed.")

    def load_session(self) -> tuple[UserProfile, TokenSet] | None:
        raw_payload: str | None = None

        if keyring is not None:
            try:
                raw_payload = keyring.get_password(KEYRING_SERVICE_NAME, self.account_name)
            except Exception:  # noqa: BLE001
                logger.debug("OS credential vault load failed.")
                raw_payload = None

        if not raw_payload:
            return None

        try:
            data = json.loads(raw_payload)
            if isinstance(data, dict) and data.get("version") == KEYRING_SESSION_VERSION:
                return self._load_chunked_session(data)
            if not isinstance(data, dict):
                return None
            user = UserProfile.from_dict(data.get("user", {}))
            tokens = TokenSet.from_dict(data.get("tokens", {}))
            if not user.email or not tokens.access_token:
                return None
            return user, tokens
        except (json.JSONDecodeError, TypeError, KeyError, ValueError):
            return None

    def store_oauth_config(self, config: dict[str, str]) -> None:
        """Persist the desktop-client identity needed for later token refresh."""
        if keyring is None:
            raise AuthError("The OS credential vault is unavailable; sign-in cannot persist.")
        payload = json.dumps(
            {
                "client_id": config["client_id"],
                "client_secret": config["client_secret"],
                "redirect_uri": config["redirect_uri"],
            },
            separators=(",", ":"),
        )
        try:
            keyring.set_password(KEYRING_SERVICE_NAME, self.oauth_config_account, payload)
            persisted = keyring.get_password(
                KEYRING_SERVICE_NAME, self.oauth_config_account
            )
        except Exception as error:
            raise AuthError(
                "The OS credential vault rejected the OAuth client configuration."
            ) from error
        if not persisted or not hmac.compare_digest(persisted, payload):
            raise AuthError(
                "The OS credential vault did not confirm the OAuth client configuration."
            )

    def load_oauth_config(self) -> dict[str, str] | None:
        """Load and validate the desktop-client identity saved during sign-in."""
        if keyring is None:
            return None
        try:
            payload = keyring.get_password(
                KEYRING_SERVICE_NAME, self.oauth_config_account
            )
            if not payload:
                return None
            parsed = json.loads(payload)
            if not isinstance(parsed, dict):
                return None
            return _validate_google_config(
                {str(key): str(value) for key, value in parsed.items()}
            )
        except (AuthConfigurationError, json.JSONDecodeError, TypeError, ValueError):
            return None
        except Exception:  # noqa: BLE001 - a locked vault means no saved config
            logger.debug("OS credential vault OAuth configuration load failed.")
            return None

    def clear_session(self) -> None:
        if keyring is not None:
            accounts: set[str] = set()
            try:
                manifest = keyring.get_password(KEYRING_SERVICE_NAME, self.account_name)
                accounts = self._manifest_accounts(manifest)
            except Exception:  # noqa: BLE001
                logger.debug("OS credential vault clear was unavailable.")
            for account in {self.account_name, self.oauth_config_account} | accounts:
                try:
                    keyring.delete_password(KEYRING_SERVICE_NAME, account)
                except Exception:  # noqa: BLE001
                    logger.debug("OS credential vault clear was unavailable.")

        if self.fallback_file.exists():
            try:
                self.fallback_file.unlink()
            except OSError:
                pass


_token_store = SecureTokenStore()


def set_token_store_workspace(workspace: Path) -> None:
    global _token_store
    _token_store = SecureTokenStore(workspace)


# --- OAuth Local Callback Server ---

class OAuthCallbackHandler(http.server.BaseHTTPRequestHandler):
    """HTTP handler to receive OAuth authorization code callback."""

    received_code: str | None = None
    received_state: str | None = None
    received_error: str | None = None

    def do_GET(self) -> None:
        parsed_url = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed_url.query)

        if "error" in params:
            OAuthCallbackHandler.received_error = params["error"][0]
            self._send_response_page(
                status=400,
                title="Authentication Cancelled",
                message="Authentication was cancelled or failed. You can close this window and return to your terminal.",
                is_success=False,
            )
            return

        if "code" in params and "state" in params:
            OAuthCallbackHandler.received_code = params["code"][0]
            OAuthCallbackHandler.received_state = params["state"][0]
            self._send_response_page(
                status=200,
                title="Authorization received",
                message="Pulse is validating the callback and securing your session. Return to your terminal to finish sign-in.",
                is_success=True,
            )
            return

        self._send_response_page(
            status=400,
            title="Invalid Request",
            message="Invalid OAuth callback request.",
            is_success=False,
        )

    def _send_response_page(self, status: int, title: str, message: str, is_success: bool) -> None:
        accent = "#36f19b" if is_success else "#ff5c72"
        accent_soft = "#1fbf78" if is_success else "#d43e55"
        state_class = "is-success" if is_success else "is-error"
        status_label = "HANDOFF READY" if is_success else "ACTION REQUIRED"
        result_label = "callback received" if is_success else "authorization interrupted"
        progress_label = (
            "validating state + securing session"
            if is_success
            else "check the terminal for details"
        )
        safe_title = html_lib.escape(title)
        safe_message = html_lib.escape(message)
        page = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <meta name="color-scheme" content="dark">
    <title>Pulse · {safe_title}</title>
    <style>
        :root {{
            color-scheme: dark;
            --bg: #070b12;
            --panel: #0c121c;
            --panel-raised: #111a27;
            --line: #243246;
            --text: #d9e2ef;
            --muted: #718096;
            --cyan: #20d8ee;
            --accent: {accent};
            --accent-soft: {accent_soft};
        }}

        * {{ box-sizing: border-box; }}

        html, body {{ min-height: 100%; }}

        body {{
            margin: 0;
            min-height: 100vh;
            display: flex;
            align-items: center;
            justify-content: center;
            overflow: hidden;
            padding: 28px;
            background:
                radial-gradient(circle at 50% 42%, rgba(32, 216, 238, .07), transparent 36%),
                linear-gradient(180deg, #080d16 0%, var(--bg) 100%);
            color: var(--text);
            font-family: "Cascadia Code", "SFMono-Regular", Consolas, "Liberation Mono", monospace;
        }}

        body::before {{
            position: fixed;
            inset: 0;
            pointer-events: none;
            content: "";
            opacity: .22;
            background-image:
                linear-gradient(rgba(255,255,255,.018) 1px, transparent 1px),
                linear-gradient(90deg, rgba(255,255,255,.014) 1px, transparent 1px);
            background-size: 24px 24px;
        }}

        .terminal {{
            position: relative;
            width: min(720px, 100%);
            overflow: hidden;
            border: 1px solid var(--line);
            border-radius: 14px;
            background: rgba(12, 18, 28, .96);
            box-shadow:
                0 32px 90px rgba(0, 0, 0, .55),
                0 0 0 1px rgba(255, 255, 255, .025) inset,
                0 0 42px color-mix(in srgb, var(--accent) 8%, transparent);
            animation: terminal-in 650ms cubic-bezier(.2,.8,.2,1) both;
        }}

        .terminal::after {{
            position: absolute;
            inset: 0;
            pointer-events: none;
            content: "";
            background: linear-gradient(transparent 50%, rgba(255,255,255,.012) 50%);
            background-size: 100% 4px;
            opacity: .35;
        }}

        .terminal-bar {{
            position: relative;
            z-index: 1;
            min-height: 48px;
            display: grid;
            grid-template-columns: 1fr auto 1fr;
            align-items: center;
            gap: 16px;
            padding: 0 18px;
            border-bottom: 1px solid var(--line);
            background: var(--panel-raised);
            font-size: 11px;
            letter-spacing: .08em;
        }}

        .window-controls {{ display: flex; gap: 7px; }}
        .window-controls i {{ width: 8px; height: 8px; border-radius: 50%; background: #425069; }}
        .window-controls i:first-child {{ background: #ff5f57; }}
        .window-controls i:nth-child(2) {{ background: #febc2e; }}
        .window-controls i:last-child {{ background: #28c840; }}

        .path {{ color: var(--muted); white-space: nowrap; }}
        .path strong {{ color: var(--cyan); font-weight: 600; }}

        .status {{
            justify-self: end;
            padding: 5px 8px;
            border: 1px solid color-mix(in srgb, var(--accent) 38%, transparent);
            border-radius: 4px;
            color: var(--accent);
            background: color-mix(in srgb, var(--accent) 7%, transparent);
            font-size: 9px;
            font-weight: 700;
            letter-spacing: .12em;
        }}

        .terminal-body {{
            position: relative;
            z-index: 1;
            padding: clamp(30px, 6vw, 54px);
        }}

        .brand {{
            display: flex;
            align-items: center;
            gap: 13px;
            margin-bottom: 34px;
            color: #f3f7fb;
            font-size: 14px;
            font-weight: 700;
            letter-spacing: .14em;
            text-transform: uppercase;
        }}

        .brand-mark {{
            width: 34px;
            height: 34px;
            display: grid;
            place-items: center;
            border: 1px solid color-mix(in srgb, var(--cyan) 50%, transparent);
            border-radius: 6px;
            color: var(--cyan);
            background: rgba(32, 216, 238, .06);
            box-shadow: 0 0 18px rgba(32, 216, 238, .1);
        }}

        .brand span {{ color: var(--muted); font-weight: 500; }}

        .command {{
            padding-bottom: 22px;
            border-bottom: 1px dashed #233044;
            color: #bdc9d8;
            font-size: clamp(13px, 2.2vw, 16px);
            line-height: 1.7;
        }}

        .prompt {{ color: var(--cyan); }}

        .caret {{
            display: inline-block;
            width: .58em;
            height: 1.08em;
            margin-left: 7px;
            vertical-align: -.17em;
            background: var(--cyan);
            animation: blink 1s steps(1, end) infinite;
        }}

        .output {{ padding: 22px 0 8px; }}
        .output-line {{
            display: flex;
            gap: 13px;
            margin: 0 0 13px;
            color: #9eacbd;
            font-size: clamp(12px, 2vw, 14px);
            line-height: 1.65;
            opacity: 0;
            transform: translateY(5px);
            animation: line-in 360ms ease-out forwards;
        }}
        .output-line:nth-child(1) {{ animation-delay: 500ms; }}
        .output-line:nth-child(2) {{ animation-delay: 850ms; }}
        .output-line:nth-child(3) {{ animation-delay: 1200ms; }}
        .label {{ min-width: 62px; color: var(--muted); }}
        .output-line.result {{ color: var(--accent); }}
        .output-line.result .label {{ color: var(--accent-soft); }}
        .output-line.next {{ color: #c7d2df; }}
        .output-line.next .label {{ color: var(--cyan); }}

        .message {{
            margin: 25px 0 0;
            padding: 16px 18px;
            border-left: 2px solid var(--accent);
            background: color-mix(in srgb, var(--accent) 5%, transparent);
            color: #8797aa;
            font-size: 12px;
            line-height: 1.65;
        }}

        .message strong {{ color: var(--text); font-weight: 500; }}

        @keyframes terminal-in {{
            from {{ opacity: 0; transform: translateY(14px) scale(.985); }}
            to {{ opacity: 1; transform: translateY(0) scale(1); }}
        }}
        @keyframes line-in {{ to {{ opacity: 1; transform: translateY(0); }} }}
        @keyframes blink {{ 0%, 48% {{ opacity: 1; }} 49%, 100% {{ opacity: 0; }} }}

        @media (max-width: 540px) {{
            body {{ padding: 14px; align-items: flex-start; padding-top: 12vh; }}
            .terminal-bar {{ grid-template-columns: auto 1fr; }}
            .path {{ text-align: right; overflow: hidden; text-overflow: ellipsis; }}
            .status {{ display: none; }}
            .terminal-body {{ padding: 28px 23px 26px; }}
            .label {{ min-width: 50px; }}
        }}

        @media (prefers-reduced-motion: reduce) {{
            *, *::before, *::after {{
                animation-duration: .01ms !important;
                animation-delay: 0ms !important;
                animation-iteration-count: 1 !important;
            }}
        }}
    </style>
</head>
<body class="{state_class}">
    <main class="terminal" role="status" aria-live="polite">
        <header class="terminal-bar">
            <div class="window-controls" aria-hidden="true"><i></i><i></i><i></i></div>
            <div class="path"><strong>pulse</strong> ~/auth/google</div>
            <div class="status">{status_label}</div>
        </header>
        <section class="terminal-body">
            <div class="brand">
                <div class="brand-mark" aria-hidden="true">&gt;_</div>
                <div>Pulse <span>/ OAuth</span></div>
            </div>
            <div class="command">
                <span class="prompt">$</span> pulse auth --provider google<span class="caret" aria-hidden="true"></span>
            </div>
            <div class="output">
                <p class="output-line result"><span class="label">[ok]</span><span>{result_label}</span></p>
                <p class="output-line"><span class="label">[..]</span><span>{progress_label}</span></p>
                <p class="output-line next"><span class="label">[-&gt;]</span><span>{safe_title}</span></p>
            </div>
            <p class="message"><strong>terminal:</strong> {safe_message}</p>
        </section>
    </main>
</body>
</html>"""
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(page.encode("utf-8"))

    def log_message(self, format: str, *args: Any) -> None:
        pass


class SingleRequestHTTPServer(socketserver.TCPServer):
    allow_reuse_address = True


# --- OAuth Client Helper ---

def _packaged_google_config() -> dict[str, str]:
    """Load the public OAuth identity embedded in the installed distribution."""
    try:
        resource = importlib.resources.files("pulse").joinpath(PRODUCT_OAUTH_RESOURCE)
        raw = resource.read_text(encoding="utf-8")
        if len(raw.encode("utf-8")) > 4096:
            raise AuthConfigurationError("The packaged Google sign-in configuration is invalid.")
        parsed = json.loads(raw)
    except AuthConfigurationError:
        raise
    except (FileNotFoundError, ModuleNotFoundError, OSError, TypeError, json.JSONDecodeError):
        return {}

    if not isinstance(parsed, dict):
        raise AuthConfigurationError("The packaged Google sign-in configuration is invalid.")
    return {
        "client_id": str(parsed.get("client_id", "")).strip(),
        "client_secret": str(parsed.get("client_secret", "")).strip(),
        "redirect_uri": str(parsed.get("redirect_uri", DEFAULT_REDIRECT_URI)).strip(),
    }


def _validated_redirect_uri(value: str) -> str:
    try:
        parsed = urllib.parse.urlparse(value)
        port = parsed.port
    except ValueError as error:
        raise AuthConfigurationError(
            "The Google sign-in callback configuration in this installation is invalid."
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
        raise AuthConfigurationError(
            "The Google sign-in callback configuration in this installation is invalid."
        )
    return value


def _validate_google_config(config: dict[str, str]) -> dict[str, str]:
    client_id = config.get("client_id", "").strip()
    client_secret = config.get("client_secret", "").strip()
    redirect_uri = config.get("redirect_uri", DEFAULT_REDIRECT_URI).strip()
    if (
        client_id.lower() in _UNCONFIGURED_CLIENT_IDS
        or not _GOOGLE_CLIENT_ID.fullmatch(client_id)
    ):
        raise AuthConfigurationError(
            "This Pulse installation does not include a valid Google sign-in client."
        )
    if not _GOOGLE_CLIENT_SECRET.fullmatch(client_secret):
        raise AuthConfigurationError(
            "This Pulse installation does not include valid Google Desktop client credentials."
        )

    return {
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": _validated_redirect_uri(redirect_uri or DEFAULT_REDIRECT_URI),
    }


def get_google_config() -> dict[str, str]:
    """Resolve the product-owned public OAuth identity without reading workspace files."""
    packaged = _packaged_google_config()
    return _validate_google_config(
        {
            "client_id": os.environ.get(PRODUCT_CLIENT_ID_ENV, "").strip()
            or packaged.get("client_id", ""),
            "client_secret": os.environ.get(PRODUCT_CLIENT_SECRET_ENV, "").strip()
            or packaged.get("client_secret", ""),
            "redirect_uri": os.environ.get(PRODUCT_REDIRECT_URI_ENV, "").strip()
            or packaged.get("redirect_uri", DEFAULT_REDIRECT_URI),
        }
    )


def build_authorization_url(
    state: str,
    code_challenge: str,
    *,
    config: dict[str, str] | None = None,
) -> str:
    config = config or get_google_config()
    client_id = config["client_id"]

    params = {
        "client_id": client_id,
        "redirect_uri": config["redirect_uri"],
        "response_type": "code",
        "scope": "openid email profile",
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    return f"https://accounts.google.com/o/oauth2/v2/auth?{urllib.parse.urlencode(params)}"


def exchange_code_for_tokens(
    code: str,
    code_verifier: str,
    *,
    config: dict[str, str] | None = None,
) -> tuple[TokenSet, UserProfile]:
    config = config or get_google_config()
    data = urllib.parse.urlencode({
        "code": code,
        "client_id": config["client_id"],
        "client_secret": config["client_secret"],
        "redirect_uri": config["redirect_uri"],
        "grant_type": "authorization_code",
        "code_verifier": code_verifier,
    }).encode("utf-8")

    req = urllib.request.Request(
        "https://oauth2.googleapis.com/token",
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            token_res = json.loads(resp.read().decode("utf-8"))
    except urllib.error.URLError as error:
        raise AuthError("Failed to exchange the authorization code with Google.") from error
    except (json.JSONDecodeError, OSError, UnicodeError) as error:
        raise AuthError("Google returned an invalid token response.") from error

    if not isinstance(token_res, dict):
        raise AuthError("Google returned an invalid token response.")

    access_token = token_res.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        raise AuthError("Token endpoint returned no access_token.")

    refresh_token = token_res.get("refresh_token")
    id_token = token_res.get("id_token")
    if refresh_token is not None and not isinstance(refresh_token, str):
        raise AuthError("Google returned an invalid refresh token.")
    if id_token is not None and not isinstance(id_token, str):
        raise AuthError("Google returned an invalid identity token.")
    try:
        expires_in = int(token_res.get("expires_in", 3600))
    except (TypeError, ValueError) as error:
        raise AuthError("Google returned an invalid token lifetime.") from error
    if expires_in <= 0:
        raise AuthError("Google returned an invalid token lifetime.")
    expires_at = time.time() + expires_in

    token_set = TokenSet(
        access_token=access_token,
        refresh_token=refresh_token,
        id_token=id_token,
        expires_at=expires_at,
    )

    user_profile = _extract_user_profile(id_token, access_token)
    return token_set, user_profile


def _extract_user_profile(id_token: str | None, access_token: str) -> UserProfile:
    # The ID token payload can be decoded for diagnostics, but it must not be
    # trusted without signature verification. The authenticated userinfo
    # endpoint is authoritative for the public CLI login identity.
    del id_token
    try:
        req = urllib.request.Request(
            "https://www.googleapis.com/oauth2/v3/userinfo",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            info = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, json.JSONDecodeError, OSError, ValueError) as error:
        raise AuthError("Could not verify the Google user profile.") from error

    email = info.get("email")
    name = info.get("name")
    picture = info.get("picture")
    sub = info.get("sub")
    if not email or not sub:
        raise AuthError("Google userinfo response did not include email and subject identifiers.")

    return UserProfile(
        email=email,
        name=name or email.split("@")[0],
        picture=picture,
        sub=sub,
    )


def revoke_token(token: str) -> bool:
    if not token:
        return False
    try:
        data = urllib.parse.urlencode({"token": token}).encode("utf-8")
        req = urllib.request.Request(
            "https://oauth2.googleapis.com/revoke",
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return int(resp.status) == 200
    except (urllib.error.URLError, OSError, ValueError):
        return False


# --- Primary Authentication Manager ---

class AuthenticationManager:
    """Primary Authentication Manager class wrapping OAuth 2.0 PKCE, Keyring, and session logic."""

    def __init__(self, workspace: Path | None = None) -> None:
        self.workspace = (workspace or Path.cwd()).resolve()
        set_token_store_workspace(self.workspace)
        self.database_path = self.workspace / ".agent" / "pulse-auth.sqlite3"

    def is_authenticated(self) -> bool:
        session = _token_store.load_session()
        if not session:
            return False

        _, tokens = session
        if not tokens.is_expired:
            if _token_store.load_oauth_config() is None:
                try:
                    _token_store.store_oauth_config(get_google_config())
                except AuthError:
                    # A valid access token remains usable even when an older
                    # installation cannot migrate its refresh configuration.
                    logger.debug("OAuth refresh configuration migration was unavailable.")
            return True

        return self.refresh_google_token()

    def get_current_user(self) -> UserProfile | None:
        if not self.is_authenticated():
            return None
        session = _token_store.load_session()
        if session:
            return session[0]
        return None

    def current_user(self) -> str | None:
        user = self.get_current_user()
        return user.email.split("@")[0] if user else None

    def get_current_user_info(self) -> tuple[str, str | None, str | None] | None:
        user = self.get_current_user()
        if not user:
            return None
        username = user.email.split("@")[0]
        return (username, user.name, user.email)

    def logout(self) -> None:
        session = _token_store.load_session()
        if session:
            _, tokens = session
            if tokens.refresh_token:
                revoke_token(tokens.refresh_token)
            elif tokens.access_token:
                revoke_token(tokens.access_token)

        _token_store.clear_session()

    def refresh_google_token(self, username: str | None = None) -> bool:
        session = _token_store.load_session()
        if not session:
            return False

        user, tokens = session
        if not tokens.refresh_token:
            return False

        try:
            config = get_google_config()
        except AuthConfigurationError:
            stored_config = _token_store.load_oauth_config()
            if stored_config is None:
                return False
            config = stored_config
        data = urllib.parse.urlencode({
            "client_id": config["client_id"],
            "client_secret": config["client_secret"],
            "refresh_token": tokens.refresh_token,
            "grant_type": "refresh_token",
        }).encode("utf-8")

        req = urllib.request.Request(
            "https://oauth2.googleapis.com/token",
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )

        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                token_res = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, json.JSONDecodeError, OSError, UnicodeError):
            return False
        if not isinstance(token_res, dict):
            return False

        new_access_token = token_res.get("access_token")
        if not isinstance(new_access_token, str) or not new_access_token:
            return False

        try:
            expires_in = int(token_res.get("expires_in", 3600))
        except (TypeError, ValueError):
            return False
        if expires_in <= 0:
            return False
        expires_at = time.time() + expires_in
        new_refresh_token = token_res.get("refresh_token") or tokens.refresh_token
        new_id_token = token_res.get("id_token") or tokens.id_token
        if not isinstance(new_refresh_token, (str, type(None))) or not isinstance(
            new_id_token, (str, type(None))
        ):
            return False

        updated_tokens = TokenSet(
            access_token=new_access_token,
            refresh_token=new_refresh_token,
            id_token=new_id_token,
            expires_at=expires_at,
        )

        _token_store.store_session(user, updated_tokens)
        return True

    def get_google_config(self) -> dict[str, str]:
        return get_google_config()

    def get_access_token(self, auto_refresh: bool = True) -> str | None:
        session = _token_store.load_session()
        if not session:
            return None
        _, tokens = session
        if auto_refresh and tokens.is_expired:
            if self.refresh_google_token():
                session = _token_store.load_session()
                return session[1].access_token if session else None
            return None
        return tokens.access_token


# --- Top-Level Standalone Functions (Delegating to AuthenticationManager) ---

def is_authenticated() -> bool:
    return AuthenticationManager(_token_store.workspace).is_authenticated()


def get_current_user() -> UserProfile | None:
    return AuthenticationManager(_token_store.workspace).get_current_user()


def logout() -> bool:
    AuthenticationManager(_token_store.workspace).logout()
    return True


def refresh_session() -> bool:
    return AuthenticationManager(_token_store.workspace).refresh_google_token()


def login(timeout_seconds: int = 120) -> UserProfile | None:
    """Execute OAuth 2.0 PKCE browser authorization flow.

    Args:
        timeout_seconds: Maximum seconds to wait for browser callback.

    Returns:
        UserProfile of authenticated user.

    Raises:
        AuthError, AuthTimeoutError, StateMismatchError, UserCancelledError
    """
    if is_authenticated():
        return get_current_user()

    state = generate_state()
    code_verifier, code_challenge = generate_pkce_pair()
    config = get_google_config()
    parsed_redirect = urllib.parse.urlparse(config["redirect_uri"])
    requested_port = parsed_redirect.port or 0

    OAuthCallbackHandler.received_code = None
    OAuthCallbackHandler.received_state = None
    OAuthCallbackHandler.received_error = None

    try:
        server = SingleRequestHTTPServer(("127.0.0.1", requested_port), OAuthCallbackHandler)
    except OSError as e:
        port_label = str(requested_port) if requested_port else "an available port"
        raise AuthError(f"Could not start the local sign-in listener on {port_label}.") from e

    bound_port = int(server.server_address[1])
    redirect_uri = urllib.parse.urlunparse(
        parsed_redirect._replace(netloc=f"127.0.0.1:{bound_port}")
    )
    flow_config = {**config, "redirect_uri": redirect_uri}
    auth_url = build_authorization_url(state, code_challenge, config=flow_config)

    server.timeout = timeout_seconds

    print("Pulse Authentication\n")
    print("Opening your browser...\n")
    print("If your browser doesn't open automatically, visit:\n")
    print(f"{auth_url}\n")
    print("Waiting for authentication...\n")

    try:
        webbrowser.open(auth_url)
    except (webbrowser.Error, OSError) as err:
        logger.debug(f"Webbrowser open failed: {err}")

    try:
        server.handle_request()
    finally:
        server.server_close()

    if OAuthCallbackHandler.received_error:
        raise UserCancelledError("Authentication was cancelled in browser.")

    code = OAuthCallbackHandler.received_code
    cb_state = OAuthCallbackHandler.received_state

    if not code:
        raise AuthTimeoutError("Authentication timed out waiting for browser login callback.")

    if cb_state != state:
        raise StateMismatchError("OAuth state verification failed. Possible CSRF attack.")

    tokens, user_profile = exchange_code_for_tokens(
        code,
        code_verifier,
        config=flow_config,
    )
    # Source installs may receive the desktop-client identity through temporary
    # environment variables. Save it beside the session in the OS vault so a
    # later process can refresh the token without those variables.
    _token_store.store_oauth_config(config)
    try:
        _token_store.store_session(user_profile, tokens)
    except AuthError:
        _token_store.clear_session()
        raise
    return user_profile
