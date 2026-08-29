import json
import time

import pytest

from pulse.auth import (
    AuthenticationManager,
    AuthError,
    SecureTokenStore,
    TokenSet,
    UserProfile,
    build_authorization_url,
    decode_jwt_payload,
    exchange_code_for_tokens,
    generate_pkce_pair,
    generate_state,
    get_current_user,
    is_authenticated,
    logout,
    refresh_session,
    set_token_store_workspace,
)


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


def test_is_authenticated_initial_state(auth_workspace):
    assert is_authenticated() is False
    assert get_current_user() is None


def test_authorization_url_construction(monkeypatch):
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "my_test_client_id")
    monkeypatch.setenv("GOOGLE_REDIRECT_URI", "http://localhost:8080")

    state = "test_state_123"
    code_challenge = "test_challenge_456"

    url = build_authorization_url(state, code_challenge)
    assert "https://accounts.google.com/o/oauth2/v2/auth" in url
    assert "client_id=my_test_client_id" in url
    assert "state=test_state_123" in url
    assert "code_challenge=test_challenge_456" in url
    assert "code_challenge_method=S256" in url


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
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "client123")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "secret456")

    id_token = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiI5ODciLCJlbWFpbCI6ImFsaWNlQGV4YW1wbGUuY29tIiwibmFtZSI6IkFsaWNlIn0.sig"

    def mock_urlopen(req, *args, **kwargs):
        if "userinfo" in req.full_url:
            return MockHTTPResponse(json.dumps({
                "email": "alice@example.com",
                "name": "Alice",
                "sub": "987",
            }).encode("utf-8"))
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
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "client123")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "secret456")
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


def test_refresh_session_success(auth_workspace, monkeypatch):
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "client123")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "secret456")

    store = SecureTokenStore(auth_workspace)
    user = UserProfile(email="bob@example.com", name="Bob", sub="555")
    # Expired tokens
    tokens = TokenSet(access_token="old_acc", refresh_token="valid_ref", expires_at=time.time() - 100)
    store.store_session(user, tokens)

    def mock_urlopen_refresh(req, *args, **kwargs):
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
