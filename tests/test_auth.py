import io
import json
import time
import urllib.parse

import pytest

from pulse.auth import (
    AuthConfigurationError,
    AuthenticationManager,
    AuthError,
    OAuthCallbackHandler,
    SecureTokenStore,
    TokenSet,
    UserProfile,
    build_authorization_url,
    decode_jwt_payload,
    exchange_code_for_tokens,
    generate_pkce_pair,
    generate_state,
    get_current_user,
    get_google_config,
    is_authenticated,
    login,
    logout,
    refresh_session,
    set_token_store_workspace,
)

TEST_CLIENT_SECRET = "GOCSPX-abcdefghijklmnopqrstuvwxyz123456"


@pytest.fixture(autouse=True)
def google_desktop_client_credential(monkeypatch):
    monkeypatch.setenv("PULSE_GOOGLE_CLIENT_SECRET", TEST_CLIENT_SECRET)


@pytest.fixture
def auth_workspace(tmp_path, monkeypatch):
    """Fixture initializing temporary workspace for auth token store."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr("pulse.auth.revoke_token", lambda _token: True)
    set_token_store_workspace(workspace)
    yield workspace
    logout()


def test_pkce_generation():
    verifier1, challenge1 = generate_pkce_pair()
    verifier2, challenge2 = generate_pkce_pair()

    assert len(verifier1) >= 43
    assert len(challenge1) > 0
    assert verifier1 != verifier2
    assert challenge1 != challenge2


def test_state_generation():
    state1 = generate_state()
    state2 = generate_state()

    assert len(state1) >= 32
    assert state1 != state2


def test_oauth_callback_uses_terminal_ui_and_accurate_handoff_copy():
    handler = object.__new__(OAuthCallbackHandler)
    handler.path = "/?code=authorization-code&state=callback-state"
    handler.wfile = io.BytesIO()
    responses: list[int] = []
    headers: dict[str, str] = {}
    handler.send_response = responses.append
    handler.send_header = headers.__setitem__
    handler.end_headers = lambda: None

    handler.do_GET()

    page = handler.wfile.getvalue().decode("utf-8")
    assert responses == [200]
    assert headers["Cache-Control"] == "no-store"
    assert headers["Content-Security-Policy"] == "default-src 'none'; style-src 'unsafe-inline'"
    assert "pulse auth --provider google" in page
    assert "Authorization received" in page
    assert "validating state + securing session" in page
    assert "Authentication Successful" not in page
    assert "@keyframes terminal-in" in page
    assert "prefers-reduced-motion" in page


def test_oauth_callback_page_escapes_displayed_error_text():
    handler = object.__new__(OAuthCallbackHandler)
    handler.wfile = io.BytesIO()
    handler.send_response = lambda _status: None
    handler.send_header = lambda _name, _value: None
    handler.end_headers = lambda: None

    handler._send_response_page(
        status=400,
        title="Invalid <script>alert(1)</script>",
        message="Return & retry",
        is_success=False,
    )

    page = handler.wfile.getvalue().decode("utf-8")
    assert "<script>" not in page
    assert "Invalid &lt;script&gt;alert(1)&lt;/script&gt;" in page
    assert "Return &amp; retry" in page
    assert "ACTION REQUIRED" in page


def test_jwt_payload_decoding():
    # Header: {"alg":"HS256","typ":"JWT"} -> eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9
    # Payload: {"sub":"12345","email":"test@example.com","name":"Test User"}
    # -> eyJzdWIiOiIxMjM0NSIsImVtYWlsIjoidGVzdEBleGFtcGxlLmNvbSIsIm5hbWUiOiJUZXN0IFVzZXIifQ
    raw_jwt = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NSIsImVtYWlsIjoidGVzdEBleGFtcGxlLmNvbSIsIm5hbWUiOiJUZXN0IFVzZXIifQ.signature"
    payload = decode_jwt_payload(raw_jwt)

    assert payload.get("sub") == "12345"
    assert payload.get("email") == "test@example.com"
    assert payload.get("name") == "Test User"


def test_secure_token_store(auth_workspace):
    store = SecureTokenStore(auth_workspace)
    user = UserProfile(email="test@example.com", name="Test", sub="123")
    tokens = TokenSet(access_token="acc123", refresh_token="ref456", expires_at=time.time() + 3600)

    store.store_session(user, tokens)

    loaded = store.load_session()
    assert loaded is not None
    loaded_user, loaded_tokens = loaded

    assert loaded_user.email == "test@example.com"
    assert loaded_user.name == "Test"
    assert loaded_user.sub == "123"
    assert loaded_tokens.access_token == "acc123"
    assert loaded_tokens.refresh_token == "ref456"

    store.clear_session()
    assert store.load_session() is None


def test_secure_token_store_chunks_sessions_that_exceed_windows_vault_limit(
    auth_workspace,
    memory_auth_keyring,
):
    store = SecureTokenStore(auth_workspace)
    user = UserProfile(email="large-session@example.com", name="Large Session", sub="123")
    tokens = TokenSet(
        access_token="access-" + "a" * 900,
        refresh_token="refresh-" + "r" * 300,
        id_token="id-token-" + "i" * 3500,
        expires_at=time.time() + 3600,
    )
    old_combined_payload = json.dumps({"user": user.to_dict(), "tokens": tokens.to_dict()})
    assert len(old_combined_payload.encode("utf-16-le")) > 2560

    store.store_session(user, tokens)

    stored_values = [
        value
        for (service, account), value in memory_auth_keyring.credentials.items()
        if service == "pulse-cli" and account.startswith(store.account_name)
    ]
    assert len(stored_values) > 4
    assert all(len(value.encode("utf-16-le")) <= 2000 for value in stored_values)
    assert store.load_session() == (user, tokens)

    store.clear_session()
    assert not any(
        service == "pulse-cli" and account.startswith(store.account_name)
        for service, account in memory_auth_keyring.credentials
    )


def test_secure_token_store_reads_legacy_single_entry_session(
    auth_workspace,
    memory_auth_keyring,
):
    store = SecureTokenStore(auth_workspace)
    user = UserProfile(email="legacy@example.com", name="Legacy", sub="456")
    tokens = TokenSet(access_token="legacy-access", refresh_token="legacy-refresh")
    memory_auth_keyring.set_password(
        "pulse-cli",
        store.account_name,
        json.dumps({"user": user.to_dict(), "tokens": tokens.to_dict()}),
    )

    assert store.load_session() == (user, tokens)


def test_secure_token_store_persists_oauth_config_for_future_processes(
    auth_workspace,
):
    store = SecureTokenStore(auth_workspace)
    config = {
        "client_id": "123456789012-client.apps.googleusercontent.com",
        "client_secret": TEST_CLIENT_SECRET,
        "redirect_uri": "http://127.0.0.1:43123",
    }

    store.store_oauth_config(config)

    assert store.load_oauth_config() == config
    store.clear_session()
    assert store.load_oauth_config() is None


def test_is_authenticated_initial_state(auth_workspace):
    assert is_authenticated() is False
    assert get_current_user() is None


def test_expired_session_without_client_config_becomes_signed_out(
    auth_workspace, monkeypatch
):
    from pulse import auth

    store = SecureTokenStore(auth_workspace)
    store.store_session(
        UserProfile(email="expired@example.com"),
        TokenSet(
            access_token="expired-access",
            refresh_token="refresh-token",
            expires_at=0,
        ),
    )
    monkeypatch.delenv("PULSE_GOOGLE_CLIENT_ID", raising=False)
    monkeypatch.delenv("PULSE_GOOGLE_CLIENT_SECRET", raising=False)
    monkeypatch.setattr(auth, "_packaged_google_config", dict)

    assert AuthenticationManager(auth_workspace).is_authenticated() is False


def test_valid_legacy_session_migrates_oauth_config(auth_workspace, monkeypatch):
    store = SecureTokenStore(auth_workspace)
    store.store_session(
        UserProfile(email="legacy@example.com"),
        TokenSet(access_token="access", expires_at=time.time() + 3600),
    )
    monkeypatch.setenv(
        "PULSE_GOOGLE_CLIENT_ID",
        "123456789012-client.apps.googleusercontent.com",
    )

    assert AuthenticationManager(auth_workspace).is_authenticated() is True
    assert store.load_oauth_config() == {
        "client_id": "123456789012-client.apps.googleusercontent.com",
        "client_secret": TEST_CLIENT_SECRET,
        "redirect_uri": "http://127.0.0.1",
    }


def test_authorization_url_construction(monkeypatch):
    monkeypatch.setenv(
        "PULSE_GOOGLE_CLIENT_ID",
        "123456789012-my_test_client_id.apps.googleusercontent.com",
    )
    monkeypatch.setenv("PULSE_GOOGLE_REDIRECT_URI", "http://127.0.0.1:8080")

    state = "test_state_123"
    code_challenge = "test_challenge_456"

    url = build_authorization_url(state, code_challenge)
    assert "https://accounts.google.com/o/oauth2/v2/auth" in url
    assert "client_id=123456789012-my_test_client_id.apps.googleusercontent.com" in url
    assert "state=test_state_123" in url
    assert "code_challenge=test_challenge_456" in url
    assert "code_challenge_method=S256" in url


def test_google_config_never_reads_workspace_env_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(
        "GOOGLE_CLIENT_ID=999999999999-leaked.apps.googleusercontent.com\n"
        "GOOGLE_CLIENT_SECRET=must-not-be-loaded\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("PULSE_GOOGLE_CLIENT_ID", raising=False)
    monkeypatch.delenv("PULSE_GOOGLE_CLIENT_SECRET", raising=False)
    monkeypatch.delenv("PULSE_GOOGLE_REDIRECT_URI", raising=False)

    with pytest.raises(AuthConfigurationError, match="does not include"):
        get_google_config()


def test_google_config_rejects_non_loopback_redirect(monkeypatch):
    monkeypatch.setenv(
        "PULSE_GOOGLE_CLIENT_ID",
        "123456789012-valid.apps.googleusercontent.com",
    )
    monkeypatch.setenv("PULSE_GOOGLE_REDIRECT_URI", "https://example.com/callback")

    with pytest.raises(AuthConfigurationError, match="callback configuration"):
        get_google_config()


def test_login_uses_an_available_loopback_port(
    auth_workspace, monkeypatch, capsys
):
    from pulse import auth

    captured: dict[str, str] = {}
    user = UserProfile(email="user@example.com", name="User", sub="subject")
    tokens = TokenSet(access_token="access", expires_at=time.time() + 3600)

    class FakeServer:
        def __init__(self, address, _handler):
            assert address == ("127.0.0.1", 0)
            self.server_address = ("127.0.0.1", 43123)
            self.timeout = None

        def handle_request(self):
            auth.OAuthCallbackHandler.received_code = "authorization-code"
            auth.OAuthCallbackHandler.received_state = "fixed-state"

        def server_close(self):
            captured["closed"] = "yes"

    def fake_exchange(_code, _verifier, *, config):
        captured["exchange_redirect"] = config["redirect_uri"]
        return tokens, user

    monkeypatch.setenv(
        "PULSE_GOOGLE_CLIENT_ID",
        "123456789012-client.apps.googleusercontent.com",
    )
    # A stale fixed redirect must never send the callback to IIS on port 80.
    monkeypatch.setenv("PULSE_GOOGLE_REDIRECT_URI", "http://127.0.0.1:80")
    monkeypatch.setattr(auth, "generate_state", lambda: "fixed-state")
    monkeypatch.setattr(auth, "generate_pkce_pair", lambda: ("verifier", "challenge"))
    monkeypatch.setattr(auth, "SingleRequestHTTPServer", FakeServer)
    monkeypatch.setattr(auth, "exchange_code_for_tokens", fake_exchange)
    monkeypatch.setattr(auth.webbrowser, "open", lambda url: captured.setdefault("url", url))

    assert login(timeout_seconds=1) == user
    assert "redirect_uri=http%3A%2F%2F127.0.0.1%3A43123" in captured["url"]
    assert captured["exchange_redirect"] == "http://127.0.0.1:43123"
    assert captured["closed"] == "yes"
    assert "Local callback listener: http://127.0.0.1:43123" in capsys.readouterr().out


class MockHTTPResponse:
    def __init__(self, data: bytes, status: int = 200):
        self.data = data
        self.status = status

    def read(self):
        return self.data

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        pass


def test_exchange_code_for_tokens(auth_workspace, monkeypatch):
    monkeypatch.setenv(
        "PULSE_GOOGLE_CLIENT_ID",
        "123456789012-client.apps.googleusercontent.com",
    )

    id_token = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiI5ODciLCJlbWFpbCI6ImFsaWNlQGV4YW1wbGUuY29tIiwibmFtZSI6IkFsaWNlIn0.sig"

    def mock_urlopen(req, *args, **kwargs):
        if "userinfo" in req.full_url:
            return MockHTTPResponse(json.dumps({
                "email": "alice@example.com",
                "name": "Alice",
                "sub": "987",
            }).encode("utf-8"))
        request_values = urllib.parse.parse_qs(req.data.decode("utf-8"))
        assert request_values["client_secret"] == [TEST_CLIENT_SECRET]
        assert request_values["code_verifier"] == ["verifier_456"]
        return MockHTTPResponse(json.dumps({
            "access_token": "acc_token_abc",
            "refresh_token": "ref_token_xyz",
            "id_token": id_token,
            "expires_in": 3600,
        }).encode("utf-8"))

    monkeypatch.setattr("urllib.request.urlopen", mock_urlopen)

    tokens, user = exchange_code_for_tokens("auth_code_123", "verifier_456")

    assert user.email == "alice@example.com"
    assert user.name == "Alice"
    assert user.sub == "987"
    assert tokens.access_token == "acc_token_abc"
    assert tokens.refresh_token == "ref_token_xyz"


def test_exchange_rejects_unverified_id_token_identity(auth_workspace, monkeypatch):
    monkeypatch.setenv(
        "PULSE_GOOGLE_CLIENT_ID",
        "123456789012-client.apps.googleusercontent.com",
    )
    forged = "e30.eyJzdWIiOiJhdHRhY2tlciIsImVtYWlsIjoiYXR0YWNrZXJAZXhhbXBsZS5jb20ifQ.invalid"

    def mock_urlopen(req, *args, **kwargs):
        if "userinfo" in req.full_url:
            raise OSError("userinfo unavailable")
        return MockHTTPResponse(json.dumps({
            "access_token": "access-token",
            "id_token": forged,
            "expires_in": 3600,
        }).encode("utf-8"))

    monkeypatch.setattr("urllib.request.urlopen", mock_urlopen)

    with pytest.raises(AuthError, match="Could not verify the Google user profile"):
        exchange_code_for_tokens("auth-code", "verifier")


@pytest.mark.parametrize(
    "payload, message",
    [
        (b"not-json", "invalid token response"),
        (b"[]", "invalid token response"),
        (json.dumps({"access_token": "token", "expires_in": "never"}).encode(), "lifetime"),
    ],
)
def test_exchange_maps_malformed_google_responses_to_safe_auth_errors(
    auth_workspace, monkeypatch, payload, message
):
    monkeypatch.setenv(
        "PULSE_GOOGLE_CLIENT_ID",
        "123456789012-client.apps.googleusercontent.com",
    )
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda *_args, **_kwargs: MockHTTPResponse(payload),
    )

    with pytest.raises(AuthError, match=message):
        exchange_code_for_tokens("auth-code", "verifier")


def test_refresh_session_success(auth_workspace, monkeypatch):
    monkeypatch.setenv(
        "PULSE_GOOGLE_CLIENT_ID",
        "123456789012-client.apps.googleusercontent.com",
    )

    store = SecureTokenStore(auth_workspace)
    user = UserProfile(email="bob@example.com", name="Bob", sub="555")
    # Expired tokens
    tokens = TokenSet(access_token="old_acc", refresh_token="valid_ref", expires_at=time.time() - 100)
    store.store_session(user, tokens)

    def mock_urlopen_refresh(req, *args, **kwargs):
        request_values = urllib.parse.parse_qs(req.data.decode("utf-8"))
        assert request_values["client_secret"] == [TEST_CLIENT_SECRET]
        return MockHTTPResponse(json.dumps({
            "access_token": "new_refreshed_access_token",
            "expires_in": 3600,
        }).encode("utf-8"))

    monkeypatch.setattr("urllib.request.urlopen", mock_urlopen_refresh)

    assert refresh_session() is True
    assert is_authenticated() is True

    curr_user = get_current_user()
    assert curr_user is not None
    assert curr_user.email == "bob@example.com"


def test_logout(auth_workspace):
    store = SecureTokenStore(auth_workspace)
    user = UserProfile(email="carol@example.com", name="Carol")
    tokens = TokenSet(access_token="acc", refresh_token="ref", expires_at=time.time() + 3600)
    store.store_session(user, tokens)

    assert is_authenticated() is True
    assert logout() is True
    assert is_authenticated() is False
    assert get_current_user() is None


def test_legacy_auth_manager_compatibility(auth_workspace, monkeypatch):
    manager = AuthenticationManager(auth_workspace)
    assert manager.is_authenticated() is False
    assert manager.current_user() is None

    store = SecureTokenStore(auth_workspace)
    user = UserProfile(email="dave@example.com", name="Dave", sub="777")
    tokens = TokenSet(access_token="acc", refresh_token="ref", expires_at=time.time() + 3600)
    store.store_session(user, tokens)

    assert manager.is_authenticated() is True
    assert manager.current_user() == "dave"
    info = manager.get_current_user_info()
    assert info == ("dave", "Dave", "dave@example.com")

    manager.logout()
    assert manager.is_authenticated() is False
