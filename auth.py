"""Password hashing and session helpers for DonSTU СНО."""

from __future__ import annotations

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
