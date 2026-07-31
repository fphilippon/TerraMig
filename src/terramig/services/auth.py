from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from ..persistence import Persistence, SessionRecord, UserRecord


PASSWORD_ITERATIONS = 600_000
SESSION_HOURS = 8
MAX_FAILED_ATTEMPTS = 5
LOCK_MINUTES = 5


class AuthenticationError(ValueError):
    def __init__(self, message: str, status: int = 401) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class AuthenticatedSession:
    user: UserRecord
    session: SessionRecord


@dataclass(frozen=True)
class IssuedSession:
    user: UserRecord
    token: str
    csrf_token: str
    expires_at: str


class AuthService:
    def __init__(self, persistence: Persistence) -> None:
        self.persistence = persistence
        self.session_hours = max(
            1,
            min(
                int(os.getenv("TERRAMIG_SESSION_HOURS", str(SESSION_HOURS))),
                168,
            ),
        )
        self._dummy_hash, self._dummy_salt = self._hash_password(
            secrets.token_urlsafe(24)
        )
        self.bootstrap_admin()

    def bootstrap_admin(self) -> UserRecord:
        username = os.getenv("TERRAMIG_ADMIN_USERNAME", "admin").strip() or "admin"
        password = os.getenv("TERRAMIG_ADMIN_PASSWORD", "admin")
        password_hash, salt = self._hash_password(password)
        now = self._now()
        return self.persistence.create_user_if_absent(
            UserRecord(
                id=str(uuid4()),
                username=username,
                password_hash=password_hash,
                password_salt=salt,
                password_iterations=PASSWORD_ITERATIONS,
                must_change_password=True,
                created_at=now,
                updated_at=now,
            )
        )

    def login(self, username: str, password: str) -> IssuedSession:
        user = self.persistence.get_user_by_username(username.strip())
        now = self._datetime()
        if user and user.locked_until and self._parse(user.locked_until) > now:
            raise AuthenticationError(
                "Too many failed attempts. Try again later.", status=429
            )
        password_valid = (
            self._verify_password(password, user)
            if user
            else hmac.compare_digest(
                self._hash_password(
                    password, self._dummy_salt, PASSWORD_ITERATIONS
                )[0],
                self._dummy_hash,
            )
        )
        if not user or user.disabled or not password_valid:
            if user and not user.disabled:
                user.failed_attempts += 1
                if user.failed_attempts >= MAX_FAILED_ATTEMPTS:
                    user.locked_until = (
                        now + timedelta(minutes=LOCK_MINUTES)
                    ).isoformat()
                    user.failed_attempts = 0
                user.updated_at = now.isoformat()
                self.persistence.save_user(user)
            raise AuthenticationError("Invalid username or password")
        user.failed_attempts = 0
        user.locked_until = ""
        user.updated_at = now.isoformat()
        self.persistence.save_user(user)
        return self._issue(user)

    def authenticate(self, token: str) -> AuthenticatedSession:
        if not token:
            raise AuthenticationError("Authentication required")
        token_hash = self._digest(token)
        session = self.persistence.get_session(token_hash)
        now = self._datetime()
        if not session or self._parse(session.expires_at) <= now:
            if session:
                self.persistence.delete_session(token_hash)
            raise AuthenticationError("Session expired")
        user = self.persistence.get_user(session.user_id)
        if not user or user.disabled:
            self.persistence.delete_session(token_hash)
            raise AuthenticationError("Authentication required")
        self.persistence.touch_session(token_hash, now.isoformat())
        session.last_seen_at = now.isoformat()
        return AuthenticatedSession(user, session)

    def verify_csrf(
        self, authenticated: AuthenticatedSession, cookie_token: str, header_token: str
    ) -> None:
        if (
            not cookie_token
            or not header_token
            or not hmac.compare_digest(cookie_token, header_token)
            or not hmac.compare_digest(
                self._digest(header_token), authenticated.session.csrf_hash
            )
        ):
            raise AuthenticationError("CSRF validation failed", status=403)

    def change_password(
        self,
        authenticated: AuthenticatedSession,
        current_password: str,
        new_password: str,
    ) -> IssuedSession:
        user = authenticated.user
        if not self._verify_password(current_password, user):
            raise AuthenticationError("Current password is incorrect", status=403)
        self._validate_new_password(user.username, new_password)
        if self._verify_password(new_password, user):
            raise AuthenticationError(
                "New password must differ from the current password", status=400
            )
        password_hash, salt = self._hash_password(new_password)
        user.password_hash = password_hash
        user.password_salt = salt
        user.password_iterations = PASSWORD_ITERATIONS
        user.must_change_password = False
        user.failed_attempts = 0
        user.locked_until = ""
        user.updated_at = self._now()
        self.persistence.save_user(user)
        self.persistence.delete_user_sessions(user.id)
        return self._issue(user)

    def logout(self, token: str) -> None:
        if token:
            self.persistence.delete_session(self._digest(token))

    def public_user(self, user: UserRecord) -> dict[str, object]:
        return {
            "id": user.id,
            "username": user.username,
            "must_change_password": user.must_change_password,
        }

    def _issue(self, user: UserRecord) -> IssuedSession:
        self.persistence.delete_expired_sessions(self._now())
        raw_token = secrets.token_urlsafe(48)
        csrf_token = secrets.token_urlsafe(32)
        now = self._datetime()
        expires = now + timedelta(hours=self.session_hours)
        session = SessionRecord(
            token_hash=self._digest(raw_token),
            user_id=user.id,
            csrf_hash=self._digest(csrf_token),
            created_at=now.isoformat(),
            expires_at=expires.isoformat(),
            last_seen_at=now.isoformat(),
        )
        self.persistence.save_session(session)
        return IssuedSession(user, raw_token, csrf_token, session.expires_at)

    @staticmethod
    def _validate_new_password(username: str, password: str) -> None:
        if len(password) < 12:
            raise AuthenticationError(
                "New password must contain at least 12 characters", status=400
            )
        if username.casefold() in password.casefold():
            raise AuthenticationError(
                "New password must not contain the username", status=400
            )
        classes = (
            any(character.islower() for character in password),
            any(character.isupper() for character in password),
            any(character.isdigit() for character in password),
            any(not character.isalnum() for character in password),
        )
        if sum(classes) < 3:
            raise AuthenticationError(
                "New password must use at least three character classes", status=400
            )

    @staticmethod
    def _hash_password(
        password: str, salt: str = "", iterations: int = PASSWORD_ITERATIONS
    ) -> tuple[str, str]:
        raw_salt = (
            base64.urlsafe_b64decode(salt.encode()) if salt else secrets.token_bytes(24)
        )
        digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), raw_salt, iterations
        )
        return (
            base64.urlsafe_b64encode(digest).decode(),
            base64.urlsafe_b64encode(raw_salt).decode(),
        )

    @classmethod
    def _verify_password(cls, password: str, user: UserRecord) -> bool:
        candidate, _ = cls._hash_password(
            password, user.password_salt, user.password_iterations
        )
        return hmac.compare_digest(candidate, user.password_hash)

    @staticmethod
    def _digest(value: str) -> str:
        return hashlib.sha256(value.encode()).hexdigest()

    @staticmethod
    def _datetime() -> datetime:
        return datetime.now(timezone.utc)

    @classmethod
    def _now(cls) -> str:
        return cls._datetime().isoformat()

    @staticmethod
    def _parse(value: str) -> datetime:
        parsed = datetime.fromisoformat(value)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
