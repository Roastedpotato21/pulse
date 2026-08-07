"""Authentication management for Pulse.

Provides user registration, secure password hashing, SQLite user storage,
and session tracking.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class User:
    username: str
    password_hash: bytes | None
    salt: bytes | None
    email: str | None = None
    google_id: str | None = None
    auth_provider: str = 'local'


class AuthenticationManager:
    """Manages user registration, login, and session state."""

    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace.resolve()
        self.database_path = self.workspace / ".agent" / "pulse-auth.sqlite3"
        self.session_file = self.workspace / ".agent" / ".pulse-session.json"
        self._current_user: str | None = None
        self._init_db()
        self._load_session()

    def _init_db(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.database_path) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            cursor = conn.execute("PRAGMA table_info(users)")
            columns = [row[1] for row in cursor.fetchall()]
            
            if columns and "google_id" not in columns:
                conn.execute("ALTER TABLE users RENAME TO users_old")
                conn.execute(
                    """
                    CREATE TABLE users (
                        username TEXT PRIMARY KEY,
                        password_hash BLOB,
                        salt BLOB,
                        email TEXT,
                        google_id TEXT,
                        auth_provider TEXT NOT NULL DEFAULT 'local',
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                )
                conn.execute(
                    """
                    INSERT INTO users (username, password_hash, salt, auth_provider)
                    SELECT username, password_hash, salt, 'local' FROM users_old
                    """
                )
                conn.execute("DROP TABLE users_old")
            elif not columns:
                conn.execute(
                    """
                    CREATE TABLE users (
                        username TEXT PRIMARY KEY,
                        password_hash BLOB,
                        salt BLOB,
                        email TEXT,
                        google_id TEXT,
                        auth_provider TEXT NOT NULL DEFAULT 'local',
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                )

    def _load_session(self) -> None:
        if self.session_file.exists():
            try:
                with open(self.session_file, "r") as f:
                    data = json.load(f)
                    self._current_user = data.get("username")
            except (json.JSONDecodeError, OSError):
                self._current_user = None
        else:
            self._current_user = None

    def _save_session(self) -> None:
        try:
            self.session_file.parent.mkdir(parents=True, exist_ok=True)
            if self._current_user:
                with open(self.session_file, "w") as f:
                    json.dump({"username": self._current_user}, f)
            else:
                if self.session_file.exists():
                    self.session_file.unlink()
        except OSError:
            pass

    def _hash_password(self, password: str, salt: bytes) -> bytes:
        return hashlib.pbkdf2_hmac(
            'sha256',
            password.encode('utf-8'),
            salt,
            100000
        )

    def register(self, username: str, password: str) -> bool:
        """Register a new user. Returns True if successful, False if user exists."""
        if not username or not password:
            raise ValueError("Username and password are required")
        
        salt = os.urandom(32)
        password_hash = self._hash_password(password, salt)

        try:
            with sqlite3.connect(self.database_path) as conn:
                conn.execute(
                    "INSERT INTO users (username, password_hash, salt) VALUES (?, ?, ?)",
                    (username, password_hash, salt)
                )
            return True
        except sqlite3.IntegrityError:
            return False

    def login(self, username: str, password: str) -> bool:
        """Attempt to log in. Returns True on success, False on failure."""
        with sqlite3.connect(self.database_path) as conn:
            cursor = conn.execute(
                "SELECT password_hash, salt FROM users WHERE username = ?",
                (username,)
            )
            row = cursor.fetchone()
            
        if not row:
            return False
            
        stored_hash, salt = row
        new_hash = self._hash_password(password, salt)
        
        if new_hash == stored_hash:
            self._current_user = username
            self._save_session()
            return True
        return False

    def logout(self) -> None:
        """Log out the current user."""
        self._current_user = None
        self._save_session()

    def is_authenticated(self) -> bool:
        """Check if a user is currently logged in."""
        return self._current_user is not None

    def current_user(self) -> str | None:
        """Get the currently logged-in username."""
        return self._current_user

    def get_google_config(self) -> dict:
        return {
            "client_id": os.environ.get("GOOGLE_CLIENT_ID"),
            "client_secret": os.environ.get("GOOGLE_CLIENT_SECRET"),
            "redirect_uri": os.environ.get("GOOGLE_REDIRECT_URI", "http://localhost:8080")
        }

    def google_login_url(self) -> str:
        """Generate the Google OAuth URL."""
        config = self.get_google_config()
        if not config["client_id"]:
            raise ValueError("GOOGLE_CLIENT_ID environment variable is not set")
            
        params = {
            "client_id": config["client_id"],
            "redirect_uri": config["redirect_uri"],
            "response_type": "code",
            "scope": "openid email profile",
            "access_type": "offline",
            "prompt": "consent"
        }
        query = urllib.parse.urlencode(params)
        return f"https://accounts.google.com/o/oauth2/v2/auth?{query}"

    def exchange_google_code(self, code: str) -> bool:
        """Exchange auth code for ID token and authenticate."""
        config = self.get_google_config()
        data = urllib.parse.urlencode({
            "code": code,
            "client_id": config["client_id"] or "",
            "client_secret": config["client_secret"] or "",
            "redirect_uri": config["redirect_uri"],
            "grant_type": "authorization_code"
        }).encode('utf-8')
        
        req = urllib.request.Request("https://oauth2.googleapis.com/token", data=data)
        try:
            with urllib.request.urlopen(req) as response:
                token_res = json.loads(response.read())
        except urllib.error.URLError:
            return False
            
        id_token = token_res.get("id_token")
        if not id_token:
            return False
            
        verify_url = f"https://oauth2.googleapis.com/tokeninfo?id_token={id_token}"
        try:
            with urllib.request.urlopen(verify_url) as response:
                user_info = json.loads(response.read())
        except urllib.error.URLError:
            return False
            
        google_id = user_info.get("sub")
        email = user_info.get("email")
        if not google_id or not email:
            return False
            
        username = email.split('@')[0]
        
        with sqlite3.connect(self.database_path) as conn:
            cursor = conn.execute("SELECT username FROM users WHERE google_id = ?", (google_id,))
            row = cursor.fetchone()
            
            if row:
                self._current_user = row[0]
            else:
                base_username = username
                counter = 1
                while True:
                    cur = conn.execute("SELECT username FROM users WHERE username = ?", (username,))
                    if not cur.fetchone():
                        break
                    username = f"{base_username}{counter}"
                    counter += 1
                    
                conn.execute(
                    "INSERT INTO users (username, email, google_id, auth_provider) VALUES (?, ?, ?, 'google')",
                    (username, email, google_id)
                )
                self._current_user = username
                
        self._save_session()
        return True
