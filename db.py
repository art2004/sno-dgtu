"""Database layer for DonSTU СНО event tracking.

Backend is selected by DATABASE_URL:
  1. st.secrets["DATABASE_URL"] (Streamlit Community Cloud → Secrets)
  2. environment variable DATABASE_URL
  3. otherwise local SQLite file data/sno.db

Postgres URLs (postgres://..., postgresql://..., e.g. from Neon) are normalized
to the SQLAlchemy psycopg 3 driver (postgresql+psycopg://...).
"""

from __future__ import annotations

import os
import re
from datetime import date, datetime
from pathlib import Path
from typing import Any, Optional, Union

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

DB_PATH = Path(__file__).resolve().parent / "data" / "sno.db"

EVENT_TYPES = ("грант", "конференция", "конкурс")

# Raised on unique-constraint violations (duplicate login, duplicate participation).
# app.py catches db.DuplicateError instead of sqlite3.IntegrityError.
DuplicateError = IntegrityError

DEFAULT_ADMIN_LOGIN = "admin"
DEFAULT_ADMIN_NAME = "Админ СНО"
LOCAL_FALLBACK_ADMIN_PASSWORD = "admin123"  # only for local SQLite without ADMIN_PASSWORD

DbTarget = Optional[Union[Path, str]]

_ENGINES: dict[str, Engine] = {}


# ── Settings / URL resolution ───────────────────────────────────────────────


def get_setting(name: str, default: Optional[str] = None) -> Optional[str]:
    """Read a setting from st.secrets (if available), then env, else default."""
    try:
        import streamlit as st

        try:
            value = st.secrets.get(name)  # type: ignore[attr-defined]
        except Exception:
            value = None
        if value not in (None, ""):
            return str(value)
    except Exception:
        pass
    value = os.environ.get(name)
    if value not in (None, ""):
        return value
    return default


def normalize_url(url: str) -> str:
    url = url.strip()
    if url.startswith("postgres://"):
        url = "postgresql+psycopg://" + url[len("postgres://"):]
    elif url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://"):]
    return url


def _sqlite_url(path: Path) -> str:
    path = Path(path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    return f"sqlite:///{path.as_posix()}"


def resolve_url(db_path: DbTarget = None) -> str:
    """db_path may be a filesystem path (SQLite) or a full DB URL (tests)."""
    if db_path is not None:
        s = str(db_path)
        if "://" in s:
            return normalize_url(s)
        return _sqlite_url(Path(s))
    env_url = get_setting("DATABASE_URL")
    if env_url:
        return normalize_url(env_url)
    return _sqlite_url(DB_PATH)


def get_engine(db_path: DbTarget = None) -> Engine:
    url = resolve_url(db_path)
    eng = _ENGINES.get(url)
    if eng is not None:
        return eng
    if url.startswith("sqlite"):
        eng = create_engine(url, connect_args={"check_same_thread": False})

        @event.listens_for(eng, "connect")
        def _fk_on(dbapi_conn, _rec):  # noqa: ANN001
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA foreign_keys = ON")
            cur.close()
    else:
        # Neon closes idle connections; pre_ping + recycle keep the pool healthy.
        eng = create_engine(url, pool_pre_ping=True, pool_recycle=300, pool_size=5, max_overflow=5)
    _ENGINES[url] = eng
    return eng


def is_postgres(db_path: DbTarget = None) -> bool:
    return get_engine(db_path).dialect.name == "postgresql"


def backend_name(db_path: DbTarget = None) -> str:
    return "Postgres" if is_postgres(db_path) else "SQLite"


def normalize_title(title: str) -> str:
    """Strip, lower, collapse whitespace."""
    return re.sub(r"\s+", " ", (title or "").strip().lower())


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _rows(result) -> list[dict]:  # noqa: ANN001
    return [dict(r._mapping) for r in result]


def _one(result) -> Optional[dict]:  # noqa: ANN001
    r = result.first()
    return dict(r._mapping) if r is not None else None


def _insert_returning_id(conn, sql: str, params: dict) -> int:  # noqa: ANN001
    if conn.dialect.name == "postgresql":
        return int(conn.execute(text(sql + " RETURNING id"), params).scalar_one())
    return int(conn.execute(text(sql), params).lastrowid)


# ── Schema ──────────────────────────────────────────────────────────────────

_SCHEMA = [
    """
    CREATE TABLE IF NOT EXISTS users (
        id {pk},
        login TEXT NOT NULL UNIQUE,
        password_hash TEXT NOT NULL,
        full_name TEXT NOT NULL,
        role TEXT NOT NULL CHECK(role IN ('admin', 'member')),
        created_at TEXT NOT NULL,
        active INTEGER NOT NULL DEFAULT 1
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS events (
        id {pk},
        title TEXT NOT NULL,
        title_norm TEXT NOT NULL,
        type TEXT NOT NULL CHECK(type IN ('грант', 'конференция', 'конкурс')),
        event_date TEXT NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE(title_norm, type, event_date)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS participations (
        id {pk},
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        event_id INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
        created_at TEXT NOT NULL,
        UNIQUE(user_id, event_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_participations_event ON participations(event_id)",
]


def init_db(db_path: DbTarget = None, seed_admin: bool = True) -> None:
    """Create tables (both dialects) and optionally seed the first admin."""
    eng = get_engine(db_path)
    pk = (
        "INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY"
        if eng.dialect.name == "postgresql"
        else "INTEGER PRIMARY KEY AUTOINCREMENT"
    )
    with eng.begin() as conn:
        for stmt in _SCHEMA:
            conn.execute(text(stmt.format(pk=pk)))
    if seed_admin:
        ensure_admin(db_path)


def admin_exists(db_path: DbTarget = None) -> bool:
    with get_engine(db_path).connect() as conn:
        return conn.execute(
            text("SELECT 1 FROM users WHERE role = 'admin' LIMIT 1")
        ).first() is not None


def ensure_admin(db_path: DbTarget = None) -> str:
    """
    Create the first admin if no admin exists.

    Login: ADMIN_LOGIN (secret/env, default 'admin').
    Password: ADMIN_PASSWORD (secret/env). If missing: local SQLite falls back to
    'admin123'; on Postgres no admin is created.

    Returns: 'exists' | 'created' | 'missing_password'.
    """
    if admin_exists(db_path):
        return "exists"
    login = (get_setting("ADMIN_LOGIN") or DEFAULT_ADMIN_LOGIN).strip()
    password = get_setting("ADMIN_PASSWORD")
    if not password:
        if is_postgres(db_path):
            return "missing_password"
        password = LOCAL_FALLBACK_ADMIN_PASSWORD
    from auth import hash_password

    try:
        with get_engine(db_path).begin() as conn:
            existing = conn.execute(
                text("SELECT id FROM users WHERE login = :login"), {"login": login}
            ).first()
            if existing is not None:
                # Login taken by a member → promote to admin with configured password.
                conn.execute(
                    text(
                        "UPDATE users SET role = 'admin', active = 1, password_hash = :ph "
                        "WHERE id = :id"
                    ),
                    {"ph": hash_password(password), "id": existing[0]},
                )
            else:
                conn.execute(
                    text(
                        "INSERT INTO users (login, password_hash, full_name, role, created_at, active) "
                        "VALUES (:login, :ph, :name, 'admin', :now, 1)"
                    ),
                    {"login": login, "ph": hash_password(password),
                     "name": DEFAULT_ADMIN_NAME, "now": _now()},
                )
    except IntegrityError:
        # Concurrent session created it first.
        pass
    return "created"


# ── Users ──────────────────────────────────────────────────────────────────


def create_user(
    login: str,
    password: str,
    full_name: str,
    role: str = "member",
    db_path: DbTarget = None,
) -> int:
    """Raises DuplicateError if login is taken."""
    from auth import hash_password

    if role not in ("admin", "member"):
        raise ValueError(f"Недопустимая роль: {role}")
    with get_engine(db_path).begin() as conn:
        return _insert_returning_id(
            conn,
            "INSERT INTO users (login, password_hash, full_name, role, created_at, active) "
            "VALUES (:login, :ph, :name, :role, :now, 1)",
            {"login": login.strip(), "ph": hash_password(password),
             "name": full_name.strip(), "role": role, "now": _now()},
        )


def get_user_by_login(login: str, db_path: DbTarget = None) -> Optional[dict]:
    with get_engine(db_path).connect() as conn:
        return _one(conn.execute(
            text("SELECT * FROM users WHERE login = :login"), {"login": login.strip()}
        ))


def get_user_by_id(user_id: int, db_path: DbTarget = None) -> Optional[dict]:
    with get_engine(db_path).connect() as conn:
        return _one(conn.execute(
            text("SELECT * FROM users WHERE id = :id"), {"id": user_id}
        ))


def list_users(
    active_only: bool = False,
    role: Optional[str] = None,
    db_path: DbTarget = None,
) -> list[dict]:
    sql = "SELECT id, login, full_name, role, created_at, active FROM users WHERE 1=1"
    params: dict[str, Any] = {}
    if active_only:
        sql += " AND active = 1"
    if role:
        sql += " AND role = :role"
        params["role"] = role
    sql += " ORDER BY role DESC, LOWER(full_name)"
    with get_engine(db_path).connect() as conn:
        return _rows(conn.execute(text(sql), params))


def update_user(
    user_id: int,
    full_name: Optional[str] = None,
    login: Optional[str] = None,
    password: Optional[str] = None,
    active: Optional[bool] = None,
    db_path: DbTarget = None,
) -> None:
    """Raises DuplicateError if the new login is taken."""
    from auth import hash_password

    fields: list[str] = []
    params: dict[str, Any] = {"id": user_id}
    if full_name is not None:
        fields.append("full_name = :full_name")
        params["full_name"] = full_name.strip()
    if login is not None:
        fields.append("login = :login")
        params["login"] = login.strip()
    if password is not None and password != "":
        fields.append("password_hash = :ph")
        params["ph"] = hash_password(password)
    if active is not None:
        fields.append("active = :active")
        params["active"] = 1 if active else 0
    if not fields:
        return
    with get_engine(db_path).begin() as conn:
        conn.execute(text(f"UPDATE users SET {', '.join(fields)} WHERE id = :id"), params)


def delete_user(user_id: int, db_path: DbTarget = None) -> None:
    with get_engine(db_path).begin() as conn:
        conn.execute(text("DELETE FROM users WHERE id = :id"), {"id": user_id})


# ── Events & participations ─────────────────────────────────────────────────


def get_or_create_event(
    title: str,
    event_type: str,
    event_date: str | date,
    db_path: DbTarget = None,
) -> int:
    """Return event id; create if missing. Dedup by title_norm + type + date."""
    if event_type not in EVENT_TYPES:
        raise ValueError(f"Недопустимый тип: {event_type}")
    title = title.strip()
    title_norm = normalize_title(title)
    if not title_norm:
        raise ValueError("Название мероприятия пустое")
    if isinstance(event_date, date):
        event_date = event_date.isoformat()
    key = {"tn": title_norm, "t": event_type, "d": event_date}
    select_sql = text(
        "SELECT id FROM events WHERE title_norm = :tn AND type = :t AND event_date = :d"
    )
    eng = get_engine(db_path)
    with eng.connect() as conn:
        row = conn.execute(select_sql, key).first()
        if row:
            return int(row[0])
    try:
        with eng.begin() as conn:
            return _insert_returning_id(
                conn,
                "INSERT INTO events (title, title_norm, type, event_date, created_at) "
                "VALUES (:title, :tn, :t, :d, :now)",
                {**key, "title": title, "now": _now()},
            )
    except IntegrityError:
        # Created concurrently by another session → reuse it.
        with eng.connect() as conn:
            row = conn.execute(select_sql, key).first()
            if row:
                return int(row[0])
        raise


def add_participation(
    user_id: int,
    title: str,
    event_type: str,
    event_date: str | date,
    db_path: DbTarget = None,
) -> tuple[int, int]:
    """
    Attach user to event (create event if needed).
    Returns (participation_id, event_id).
    Raises DuplicateError if user already linked to this event.
    """
    event_id = get_or_create_event(title, event_type, event_date, db_path=db_path)
    with get_engine(db_path).begin() as conn:
        pid = _insert_returning_id(
            conn,
            "INSERT INTO participations (user_id, event_id, created_at) "
            "VALUES (:u, :e, :now)",
            {"u": user_id, "e": event_id, "now": _now()},
        )
    return pid, event_id


def delete_participation(
    participation_id: int,
    user_id: Optional[int] = None,
    db_path: DbTarget = None,
) -> bool:
    """Delete participation. If user_id given, only that user's row. Returns True if deleted."""
    with get_engine(db_path).begin() as conn:
        if user_id is not None:
            res = conn.execute(
                text("DELETE FROM participations WHERE id = :id AND user_id = :u"),
                {"id": participation_id, "u": user_id},
            )
        else:
            res = conn.execute(
                text("DELETE FROM participations WHERE id = :id"), {"id": participation_id}
            )
        return res.rowcount > 0


def list_participations_for_user(user_id: int, db_path: DbTarget = None) -> list[dict]:
    with get_engine(db_path).connect() as conn:
        return _rows(conn.execute(
            text(
                """
                SELECT p.id AS participation_id, p.created_at AS joined_at,
                       e.id AS event_id, e.title, e.type, e.event_date
                FROM participations p
                JOIN events e ON e.id = p.event_id
                WHERE p.user_id = :u
                ORDER BY e.event_date DESC, LOWER(e.title)
                """
            ),
            {"u": user_id},
        ))


def list_all_participations(
    user_id: Optional[int] = None,
    event_type: Optional[str] = None,
    event_id: Optional[int] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    db_path: DbTarget = None,
) -> list[dict]:
    sql = """
        SELECT p.id AS participation_id, p.created_at AS joined_at,
               u.id AS user_id, u.full_name, u.login,
               e.id AS event_id, e.title, e.type, e.event_date
        FROM participations p
        JOIN users u ON u.id = p.user_id
        JOIN events e ON e.id = p.event_id
        WHERE 1=1
    """
    params: dict[str, Any] = {}
    if user_id is not None:
        sql += " AND p.user_id = :u"
        params["u"] = user_id
    if event_type:
        sql += " AND e.type = :t"
        params["t"] = event_type
    if event_id is not None:
        sql += " AND e.id = :e"
        params["e"] = event_id
    if date_from:
        sql += " AND e.event_date >= :df"
        params["df"] = date_from
    if date_to:
        sql += " AND e.event_date <= :dt"
        params["dt"] = date_to
    sql += " ORDER BY e.event_date DESC, LOWER(u.full_name)"
    with get_engine(db_path).connect() as conn:
        return _rows(conn.execute(text(sql), params))


def stats_by_person(db_path: DbTarget = None) -> list[dict]:
    with get_engine(db_path).connect() as conn:
        rows = _rows(conn.execute(text(
            """
            SELECT u.id AS user_id, u.full_name, u.login,
                   COUNT(p.id) AS total,
                   SUM(CASE WHEN e.type = 'грант' THEN 1 ELSE 0 END) AS grants,
                   SUM(CASE WHEN e.type = 'конференция' THEN 1 ELSE 0 END) AS conferences,
                   SUM(CASE WHEN e.type = 'конкурс' THEN 1 ELSE 0 END) AS contests
            FROM users u
            LEFT JOIN participations p ON p.user_id = u.id
            LEFT JOIN events e ON e.id = p.event_id
            WHERE u.role = 'member' AND u.active = 1
            GROUP BY u.id, u.full_name, u.login
            ORDER BY COUNT(p.id) DESC, LOWER(u.full_name)
            """
        )))
    for r in rows:  # Postgres SUM → bigint/None; normalize to int
        for k in ("total", "grants", "conferences", "contests"):
            r[k] = int(r[k] or 0)
    return rows


def stats_by_type(db_path: DbTarget = None) -> list[dict]:
    with get_engine(db_path).connect() as conn:
        rows = _rows(conn.execute(text(
            """
            SELECT e.type, COUNT(DISTINCT e.id) AS events_count,
                   COUNT(p.id) AS participations_count
            FROM events e
            LEFT JOIN participations p ON p.event_id = e.id
            GROUP BY e.type
            ORDER BY e.type
            """
        )))
    for r in rows:
        r["events_count"] = int(r["events_count"] or 0)
        r["participations_count"] = int(r["participations_count"] or 0)
    return rows


def stats_by_event(db_path: DbTarget = None) -> list[dict]:
    with get_engine(db_path).connect() as conn:
        rows = _rows(conn.execute(text(
            """
            SELECT e.id AS event_id, e.title, e.type, e.event_date,
                   COUNT(p.id) AS participants_count
            FROM events e
            LEFT JOIN participations p ON p.event_id = e.id
            GROUP BY e.id, e.title, e.type, e.event_date
            ORDER BY e.event_date DESC, COUNT(p.id) DESC
            """
        )))
    for r in rows:
        r["participants_count"] = int(r["participants_count"] or 0)
    return rows


def count_events(db_path: DbTarget = None) -> int:
    with get_engine(db_path).connect() as conn:
        return int(conn.execute(text("SELECT COUNT(*) FROM events")).scalar_one())
