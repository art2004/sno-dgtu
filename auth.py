"""Password hashing and session helpers for DonSTU СНО."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time

import bcrypt


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(
            password.encode("utf-8"), password_hash.encode("utf-8")
        )
    except (ValueError, TypeError):
        return False


def authenticate(login: str, password: str) -> dict | None:
    """Return user dict if credentials OK and user is active, else None."""
    from db import get_user_by_login

    user = get_user_by_login(login)
    if user is None:
        return None
    if not user.get("active"):
        return None
    if not verify_password(password, user["password_hash"]):
        return None
    return user


MIN_PASSWORD_LENGTH = 8


def change_password(
    user_id: int, current: str, new: str, repeat: str, db_path=None  # noqa: ANN001
) -> tuple[bool, str]:
    """Change own password. Returns (ok, message in Russian)."""
    from db import get_user_by_id, update_user

    user = get_user_by_id(user_id, db_path=db_path)
    if user is None:
        return False, "Пользователь не найден."
    if not verify_password(current or "", user["password_hash"]):
        return False, "Текущий пароль указан неверно."
    if len(new or "") < MIN_PASSWORD_LENGTH:
        return False, f"Новый пароль должен быть не короче {MIN_PASSWORD_LENGTH} символов."
    if new != repeat:
        return False, "Новый пароль и повтор не совпадают."
    if new == current:
        return False, "Новый пароль совпадает с текущим."
    update_user(user_id, password=new, db_path=db_path)
    return True, "Пароль изменён."


# ── «Remember me» cookie: signed token, survives page refresh ────────────────
# Token = "<user_id>.<expires_unix>.<fingerprint>.<hmac>". The fingerprint is an
# HMAC of the current password hash, so any password change (own or by admin)
# makes old cookies invalid; a deleted/disabled user fails the DB lookup.
# Neither the password nor its hash is ever put into the cookie.

COOKIE_NAME = "sno_auth"
COOKIE_TTL_SECONDS = 30 * 24 * 3600  # 30 days
_SECRET_SETTING_KEY = "cookie_secret"
_SECRETS_CACHE: dict[str, bytes] = {}


def get_cookie_secret(db_path=None) -> bytes:  # noqa: ANN001
    """Signing key: COOKIE_SECRET (st.secrets / env) or a random key that is
    generated once and stored in the DB `settings` table (no new secret needed)."""
    import db
    from sqlalchemy import text

    configured = db.get_setting("COOKIE_SECRET")
    if configured:
        return configured.encode("utf-8")
    cache_key = db.resolve_url(db_path)
    if cache_key in _SECRETS_CACHE:
        return _SECRETS_CACHE[cache_key]
    with db.get_engine(db_path).begin() as conn:
        # DO NOTHING → concurrent first starts agree on the same stored key.
        conn.execute(
            text("INSERT INTO settings (key, value) VALUES (:k, :v) ON CONFLICT (key) DO NOTHING"),
            {"k": _SECRET_SETTING_KEY, "v": secrets.token_urlsafe(48)},
        )
        value = conn.execute(
            text("SELECT value FROM settings WHERE key = :k"), {"k": _SECRET_SETTING_KEY}
        ).scalar_one()
    _SECRETS_CACHE[cache_key] = value.encode("utf-8")
    return _SECRETS_CACHE[cache_key]


def _sign(key: bytes, msg: str) -> str:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).hexdigest()


def _fingerprint(key: bytes, password_hash: str) -> str:
    return _sign(key, "pw|" + password_hash)[:20]


def make_auth_token(user: dict, db_path=None, now: float | None = None) -> str:  # noqa: ANN001
    """Signed token for a user dict that contains id and password_hash."""
    key = get_cookie_secret(db_path)
    expires = int((time.time() if now is None else now) + COOKIE_TTL_SECONDS)
    body = f"{int(user['id'])}.{expires}.{_fingerprint(key, user['password_hash'])}"
    return f"{body}.{_sign(key, body)}"


def user_from_token(token: str | None, db_path=None, now: float | None = None) -> dict | None:  # noqa: ANN001
    """Return the active user for a valid, unexpired token; otherwise None."""
    from db import get_user_by_id

    if not token or not isinstance(token, str):
        return None
    parts = token.strip().split(".")
    if len(parts) != 4:
        return None
    uid, expires, fp, sig = parts
    try:
        key = get_cookie_secret(db_path)
        if not hmac.compare_digest(sig, _sign(key, f"{uid}.{expires}.{fp}")):
            return None
        if int(expires) < (time.time() if now is None else now):
            return None
        user = get_user_by_id(int(uid), db_path=db_path)
    except Exception:  # noqa: BLE001 — bad token or DB hiccup → just show login
        return None
    if user is None or not user.get("active"):
        return None
    if not hmac.compare_digest(fp, _fingerprint(key, user["password_hash"])):
        return None
    return user
