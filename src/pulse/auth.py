"""Production-Grade Authentication Management for Pulse CLI.

Implements OAuth 2.0 Authorization Code Flow with PKCE (Proof Key for Code Exchange),
secure OS credential storage via keyring, automatic silent token refresh, authenticated
Google userinfo lookup, and clean session management APIs for the Pulse developer CLI.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import http.server
import json
import logging
import os
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


# --- Exceptions ---

class AuthError(Exception):
    """Base exception for authentication errors."""


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
    return json.loads(decoded_bytes.decode("utf-8"))


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

    def store_session(self, user: UserProfile, tokens: TokenSet) -> None:
        payload = json.dumps({
            "user": user.to_dict(),
            "tokens": tokens.to_dict(),
        })

        if keyring is None:
            raise AuthError("The OS credential vault is unavailable; the session was not stored.")
        try:
            keyring.set_password(KEYRING_SERVICE_NAME, self.account_name, payload)
            persisted = keyring.get_password(KEYRING_SERVICE_NAME, self.account_name)
        except Exception as error:
            logger.debug("OS credential vault storage failed.")
            raise AuthError(
                "The OS credential vault rejected the session; no plaintext fallback was written."
            ) from error
        if not persisted or not hmac.compare_digest(persisted, payload):
            raise AuthError("The OS credential vault did not confirm session persistence.")
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
            user = UserProfile.from_dict(data.get("user", {}))
            tokens = TokenSet.from_dict(data.get("tokens", {}))
            if not user.email or not tokens.access_token:
                return None
            return user, tokens
        except (json.JSONDecodeError, TypeError, KeyError):
            return None

    def clear_session(self) -> None:
        if keyring is not None:
            try:
                keyring.delete_password(KEYRING_SERVICE_NAME, self.account_name)
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
                title="Authentication Successful",
                message="✓ Successfully authenticated with Pulse! You can close this window and return to your terminal.",
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
        color = "#22c55e" if is_success else "#ef4444"
        html = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <title>{title}</title>
    <style>
        body {{
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
            background-color: #0f172a;
            color: #f8fafc;
            display: flex;
            align-items: center;
            justify-content: center;
            height: 100vh;
            margin: 0;
        }}
        .card {{
            background-color: #1e293b;
            padding: 2.5rem;
            border-radius: 12px;
            box-shadow: 0 10px 25px rgba(0,0,0,0.5);
            text-align: center;
            max-width: 420px;
            border: 1px solid #334155;
        }}
        h1 {{ color: {color}; margin-bottom: 1rem; font-size: 1.5rem; }}
        p {{ color: #94a3b8; font-size: 1rem; line-height: 1.5; }}
    </style>
</head>
<body>
    <div class="card">
        <h1>{title}</h1>
        <p>{message}</p>
    </div>
</body>
</html>"""
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(html.encode("utf-8"))

    def log_message(self, format: str, *args: Any) -> None:
        pass


class SingleRequestHTTPServer(socketserver.TCPServer):
    allow_reuse_address = True


# --- OAuth Client Helper ---

def get_google_config() -> dict[str, str]:
    """Retrieve only the OAuth values Pulse recognizes, without mutating the process."""
    from pulse.config import load_env_file

    try:
        file_values = load_env_file(Path.cwd() / ".env")
    except (OSError, UnicodeError, ValueError):
        file_values = {}
    client_id = os.environ.get("GOOGLE_CLIENT_ID") or file_values.get("GOOGLE_CLIENT_ID", "")
    client_secret = os.environ.get("GOOGLE_CLIENT_SECRET") or file_values.get(
        "GOOGLE_CLIENT_SECRET", ""
    )
    redirect_uri = os.environ.get("GOOGLE_REDIRECT_URI") or file_values.get(
        "GOOGLE_REDIRECT_URI", "http://localhost:8080"
    )
    parsed = urllib.parse.urlparse(redirect_uri)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"localhost", "127.0.0.1", "::1"}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("GOOGLE_REDIRECT_URI must be an HTTP loopback URL without credentials or query data.")

    return {
        "client_id": client_id or "",
        "client_secret": client_secret or "",
        "redirect_uri": redirect_uri or "http://localhost:8080",
    }


def build_authorization_url(state: str, code_challenge: str) -> str:
    config = get_google_config()
    client_id = config["client_id"]
    if not client_id or client_id == "replace_me":
        raise ValueError("GOOGLE_CLIENT_ID environment variable is not set or contains default 'replace_me'")

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


def exchange_code_for_tokens(code: str, code_verifier: str) -> tuple[TokenSet, UserProfile]:
    config = get_google_config()
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

    access_token = token_res.get("access_token")
    if not access_token:
        raise AuthError("Token endpoint returned no access_token.")

    refresh_token = token_res.get("refresh_token")
    id_token = token_res.get("id_token")
    expires_in = int(token_res.get("expires_in", 3600))
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
            return resp.status == 200
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

        config = get_google_config()
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
        except (urllib.error.URLError, json.JSONDecodeError, OSError):
            return False

        new_access_token = token_res.get("access_token")
        if not new_access_token:
            return False

        expires_in = int(token_res.get("expires_in", 3600))
        expires_at = time.time() + expires_in
        new_refresh_token = token_res.get("refresh_token") or tokens.refresh_token
        new_id_token = token_res.get("id_token") or tokens.id_token

        updated_tokens = TokenSet(
            access_token=new_access_token,
            refresh_token=new_refresh_token,
            id_token=new_id_token,
            expires_at=expires_at,
        )

        _token_store.store_session(user, updated_tokens)
        return True

    def get_google_config(self) -> dict:
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
    auth_url = build_authorization_url(state, code_challenge)

    config = get_google_config()
    parsed_redirect = urllib.parse.urlparse(config.get("redirect_uri", "http://localhost:8080"))
    port = parsed_redirect.port or 8080

    OAuthCallbackHandler.received_code = None
    OAuthCallbackHandler.received_state = None
    OAuthCallbackHandler.received_error = None

    try:
        server = SingleRequestHTTPServer(("localhost", port), OAuthCallbackHandler)
    except OSError as e:
        raise AuthError(f"Could not start local HTTP server on port {port}: {e}") from e

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

    server.handle_request()
    server.server_close()

    if OAuthCallbackHandler.received_error:
        raise UserCancelledError("Authentication was cancelled in browser.")

    code = OAuthCallbackHandler.received_code
    cb_state = OAuthCallbackHandler.received_state

    if not code:
        raise AuthTimeoutError("Authentication timed out waiting for browser login callback.")

    if cb_state != state:
        raise StateMismatchError("OAuth state verification failed. Possible CSRF attack.")

    tokens, user_profile = exchange_code_for_tokens(code, code_verifier)
    _token_store.store_session(user_profile, tokens)
    return user_profile
