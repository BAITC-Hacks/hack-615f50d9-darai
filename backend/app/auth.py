"""Local accounts: password hashing, server-side sessions, CSRF, role checks."""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import Depends, Request, Response
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from .config import get_settings
from .db import get_db
from .errors import ApiError
from .models import AuthSession, Employee, User, utcnow

SESSION_COOKIE = "darai_session"
CSRF_COOKIE = "darai_csrf"
CSRF_HEADER = "X-CSRF-Token"
_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
# The only routes usable while must_change_password is set (backend paths, no /api).
PASSWORD_CHANGE_ALLOWED = {("GET", "/auth/me"), ("POST", "/auth/change-password"), ("POST", "/auth/logout")}
MIN_PASSWORD_LENGTH = 10

# scrypt parameters (RFC 7914 interactive profile, ~16 MiB memory).
_SCRYPT_N, _SCRYPT_R, _SCRYPT_P = 2**14, 8, 1


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=32)
    return "scrypt${}${}${}${}${}".format(
        _SCRYPT_N, _SCRYPT_R, _SCRYPT_P,
        base64.b64encode(salt).decode(), base64.b64encode(digest).decode(),
    )


def verify_password(password: str, encoded: str) -> bool:
    try:
        scheme, n, r, p, salt_b64, digest_b64 = encoded.split("$")
        if scheme != "scrypt":
            return False
        expected = base64.b64decode(digest_b64)
        actual = hashlib.scrypt(
            password.encode(), salt=base64.b64decode(salt_b64), n=int(n), r=int(r), p=int(p),
            dklen=len(expected),
        )
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


# A fixed hash so that unknown logins cost the same as wrong passwords.
_DUMMY_HASH = hash_password(secrets.token_urlsafe(16))


def generate_temporary_password() -> str:
    """Cryptographically random one-time password (~96 bits), shown to the admin once."""
    return secrets.token_urlsafe(12)


def _token_id(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def authenticate(db: Session, login: str, password: str) -> User:
    user = db.scalar(select(User).where(User.login == login))
    if user is None:
        verify_password(password, _DUMMY_HASH)
        raise ApiError(401, "INVALID_CREDENTIALS", "Неверный логин или пароль")
    if not verify_password(password, user.password_hash) or not user.active:
        raise ApiError(401, "INVALID_CREDENTIALS", "Неверный логин или пароль")
    return user


def create_session(db: Session, user: User, response: Response) -> str:
    settings = get_settings()
    token = secrets.token_urlsafe(32)
    csrf = secrets.token_urlsafe(32)
    now = utcnow()
    db.execute(delete(AuthSession).where(AuthSession.expires_at < now))
    db.add(AuthSession(id=_token_id(token), user_id=user.id, csrf_token=csrf, created_at=now,
                       expires_at=now + timedelta(hours=settings.session_ttl_hours)))
    db.commit()
    max_age = settings.session_ttl_hours * 3600
    response.set_cookie(SESSION_COOKIE, token, max_age=max_age, httponly=True, samesite="lax",
                        secure=settings.cookie_secure, path="/")
    response.set_cookie(CSRF_COOKIE, csrf, max_age=max_age, httponly=False, samesite="lax",
                        secure=settings.cookie_secure, path="/")
    return csrf


def destroy_session(db: Session, request: Request, response: Response) -> None:
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        db.execute(delete(AuthSession).where(AuthSession.id == _token_id(token)))
        db.commit()
    response.delete_cookie(SESSION_COOKIE, path="/")
    response.delete_cookie(CSRF_COOKIE, path="/")


def revoke_user_sessions(db: Session, user_id) -> None:
    db.execute(delete(AuthSession).where(AuthSession.user_id == user_id))


class CurrentUser:
    """Authenticated principal attached to a request."""

    def __init__(self, user: User, session: AuthSession):
        self.user = user
        self.session = session

    @property
    def id(self):
        return self.user.id

    @property
    def role(self) -> str:
        return self.user.role

    @property
    def employee_id(self):
        return self.user.employee_id

    @property
    def is_admin(self) -> bool:
        return self.user.role == "admin"


def get_current_user(request: Request, db: Session = Depends(get_db)) -> CurrentUser:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        raise ApiError(401, "UNAUTHENTICATED", "Требуется вход")
    sess = db.get(AuthSession, _token_id(token))
    now = datetime.now(timezone.utc)
    if sess is None or sess.expires_at <= now:
        raise ApiError(401, "UNAUTHENTICATED", "Сессия истекла")
    user = db.get(User, sess.user_id)
    if user is None or not user.active or (user.employee is not None and not user.employee.active):
        raise ApiError(401, "UNAUTHENTICATED", "Учётная запись недоступна")
    if request.method not in _SAFE_METHODS:
        header = request.headers.get(CSRF_HEADER, "")
        if not header or not hmac.compare_digest(header, sess.csrf_token):
            raise ApiError(403, "CSRF_FAILED", "Отсутствует или неверен CSRF-токен")
    if user.must_change_password and (request.method, request.url.path.rstrip("/")) not in PASSWORD_CHANGE_ALLOWED:
        raise ApiError(403, "PASSWORD_CHANGE_REQUIRED", "Смените временный пароль, чтобы продолжить")
    return CurrentUser(user, sess)


def require_roles(*roles: str):
    def dep(current: CurrentUser = Depends(get_current_user)) -> CurrentUser:
        if current.role not in roles:
            raise ApiError(403, "FORBIDDEN", "Недостаточно прав")
        return current

    return dep


require_admin = require_roles("admin")


def bootstrap_admin(db: Session) -> bool:
    """Create the first admin from ADMIN_LOGIN/ADMIN_PASSWORD when no users exist."""
    settings = get_settings()
    if not settings.admin_login or not settings.admin_password:
        return False
    if db.scalar(select(User.id).limit(1)) is not None:
        return False
    employee = Employee(fio="Администратор системы", position="Администратор", department="ИТ")
    db.add(employee)
    db.flush()
    db.add(User(employee_id=employee.id, login=settings.admin_login,
                password_hash=hash_password(settings.admin_password), role="admin"))
    db.commit()
    return True
