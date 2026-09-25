"""Database layer for DonSTU СНО event tracking.

Backend is selected by DATABASE_URL:
  1. st.secrets["DATABASE_URL"] (Streamlit Community Cloud → Secrets)
  2. environment variable DATABASE_URL
  3. otherwise local SQLite file data/sno.db

Postgres URLs (postgres://..., postgresql://..., e.g. from Neon) are normalized
to the SQLAlchemy psycopg 3 driver (postgresql+psycopg://...).
"""

from __future__ import annotations

import json
import os
import re
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any, Optional, Union

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError, OperationalError

DB_PATH = Path(__file__).resolve().parent / "data" / "sno.db"

EVENT_TYPES = ("грант", "конференция", "конкурс", "стипендия", "статья")
# Тип события проверяется в Python (без CHECK в БД), чтобы новые типы не требовали
# миграции ограничений. Старый CHECK на events.type снимается в _migrate_schema().

ARTICLE_TYPE = "статья"
INDEXING_OPTIONS = ("РИНЦ", "Белый список", "ВАК", "Без индексации")
ACHIEVEMENT_MAX_LEN = 64

MEETING_KINDS = ("Заседание", "Конференция", "Форум", "Круглый стол", "Другое")
DEFAULT_MEETING_KIND = "Заседание"

MEETING_FORMATS = (
    "Собрание",
    "Форсайт-сессия",
    "Круглый стол",
    "Проектная сессия",
    "Научный семинар",
    "Конференция",
    "Выступления с докладами",
    "Онлайн-заседание",
)
DEFAULT_MEETING_TIME = "13:30"

DEFAULT_SNO_NAME = "Название СНО"
DEFAULT_APPENDIX_LABEL = "Приложение Е"
DEFAULT_SIGNATORIES = [
    {"position": "Начальник Управления НИРО", "name": ""},
    {"position": "Декан факультета «…»", "name": ""},
    {"position": "Научный наставник", "name": ""},
]

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
        type TEXT NOT NULL,
        event_date TEXT NOT NULL,
        created_at TEXT NOT NULL,
        article_topic TEXT,
        indexing TEXT,
        UNIQUE(title_norm, type, event_date)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS participations (
        id {pk},
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        event_id INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
        created_at TEXT NOT NULL,
        achievement_number TEXT,
        UNIQUE(user_id, event_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_participations_event ON participations(event_id)",
    """
    CREATE TABLE IF NOT EXISTS meetings (
        id {pk},
        meeting_date DATE NOT NULL,
        meeting_time TEXT NOT NULL DEFAULT '13:30',
        location TEXT NOT NULL DEFAULT '',
        topic TEXT NOT NULL DEFAULT '',
        format TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        kind TEXT NOT NULL DEFAULT 'Заседание',
        event_id INTEGER REFERENCES events(id) ON DELETE SET NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_meetings_date ON meetings(meeting_date)",
    """
    CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """,
]


def _create_schema(db_path: DbTarget = None) -> None:
    eng = get_engine(db_path)
    pk = (
        "INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY"
        if eng.dialect.name == "postgresql"
        else "INTEGER PRIMARY KEY AUTOINCREMENT"
    )
    # CREATE ... IF NOT EXISTS only: existing tables and data are never touched,
    # new tables (meetings, settings) simply appear in old databases.
    with eng.begin() as conn:
        for stmt in _SCHEMA:
            conn.execute(text(stmt.format(pk=pk)))
    _migrate_schema(eng)


# Columns added after the first release: (table, column, DDL type). Added with
# ALTER TABLE ... ADD COLUMN only when missing → idempotent, data untouched.
_ADDED_COLUMNS = [
    ("events", "article_topic", "TEXT"),
    ("events", "indexing", "TEXT"),
    ("participations", "achievement_number", "TEXT"),
    ("meetings", "kind", "TEXT NOT NULL DEFAULT 'Заседание'"),
    ("meetings", "event_id", "INTEGER REFERENCES events(id) ON DELETE SET NULL"),
]

_EVENTS_SQLITE_REBUILD = """
PRAGMA foreign_keys = OFF;
BEGIN;
CREATE TABLE events__new (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    title_norm TEXT NOT NULL,
    type TEXT NOT NULL,
    event_date TEXT NOT NULL,
    created_at TEXT NOT NULL,
    article_topic TEXT,
    indexing TEXT,
    UNIQUE(title_norm, type, event_date)
);
INSERT INTO events__new (id, title, title_norm, type, event_date, created_at, article_topic, indexing)
    SELECT id, title, title_norm, type, event_date, created_at, {topic}, {indexing} FROM events;
DROP TABLE events;
ALTER TABLE events__new RENAME TO events;
COMMIT;
PRAGMA foreign_keys = ON;
"""


def _migrate_schema(eng: Engine) -> None:
    """Idempotent upgrades of databases created by older versions."""
    # 1) Drop the old CHECK(type IN (...)) on events.type (new types are validated in Python).
    if eng.dialect.name == "postgresql":
        with eng.begin() as conn:
            names = conn.execute(text(
                "SELECT conname FROM pg_constraint "
                "WHERE conrelid = 'events'::regclass AND contype = 'c' "
                "AND pg_get_constraintdef(oid) ILIKE '%type%'"
            )).scalars().all()
            for name in names:
                conn.execute(text(f'ALTER TABLE events DROP CONSTRAINT IF EXISTS "{name}"'))
    else:
        with eng.connect() as conn:
            ddl = conn.execute(text(
                "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'events'"
            )).scalar() or ""
        if "CHECK" in ddl.upper():
            cols = {c["name"] for c in inspect(eng).get_columns("events")}
            script = _EVENTS_SQLITE_REBUILD.format(
                topic="article_topic" if "article_topic" in cols else "NULL",
                indexing="indexing" if "indexing" in cols else "NULL",
            )
            raw = eng.raw_connection()
            try:
                # SQLite cannot drop a CHECK → documented 12-step table rebuild.
                # Same ids are kept, so participations/meetings FKs stay valid.
                raw.driver_connection.executescript(script)
                bad = raw.driver_connection.execute("PRAGMA foreign_key_check").fetchall()
                if bad:
                    raise RuntimeError(f"foreign_key_check failed after rebuild: {bad[:5]}")
            finally:
                raw.close()
            with eng.begin() as conn:
                conn.execute(text(
                    "CREATE INDEX IF NOT EXISTS idx_participations_event ON participations(event_id)"
                ))

    # 2) Add missing columns.
    insp = inspect(eng)
    existing = {t: {c["name"] for c in insp.get_columns(t)} for t in {t for t, _, _ in _ADDED_COLUMNS}}
    with eng.begin() as conn:
        for table, col, ddl in _ADDED_COLUMNS:
            if col not in existing[table]:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}"))

    # 3) One-time (idempotent) cleanup: events left with 0 participations by older versions.
    delete_orphan_events(db_path=eng)


def init_db(
    db_path: DbTarget = None,
    seed_admin: bool = True,
    attempts: int = 3,
    backoff: float = 2.0,
) -> None:
    """Create tables (both dialects) and optionally seed the first admin.

    Retries on connection errors (Neon free tier wakes up from suspend in a few
    seconds): `attempts` tries, sleeping backoff, 2*backoff, ... between them.
    """
    for attempt in range(1, attempts + 1):
        try:
            _create_schema(db_path)
            if seed_admin:
                ensure_admin(db_path)
            return
        except OperationalError:
            if attempt >= attempts:
                raise
            time.sleep(backoff * attempt)


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


class AdminGuardError(ValueError):
    """Change refused to keep at least one active admin (message in Russian)."""


def _check_admin_guard(conn, user_id: int, acting_user_id: Optional[int],  # noqa: ANN001
                       new_role: Optional[str], new_active: Optional[bool],
                       deleting: bool = False) -> None:
    row = conn.execute(
        text("SELECT role, active FROM users WHERE id = :id"), {"id": user_id}
    ).first()
    if row is None:
        return
    was_active_admin = row[0] == "admin" and bool(row[1])
    demote = new_role is not None and new_role != "admin" and row[0] == "admin"
    disable = new_active is not None and not new_active and bool(row[1])
    if acting_user_id is not None and int(acting_user_id) == int(user_id):
        if deleting:
            raise AdminGuardError("Нельзя удалить себя.")
        if demote:
            raise AdminGuardError("Нельзя снять роль админа с самого себя.")
        if disable:
            raise AdminGuardError("Нельзя отключить свою учётную запись.")
    if was_active_admin and (deleting or demote or disable):
        if conn.dialect.name == "postgresql":
            # serialize concurrent "demote each other" attempts
            conn.execute(text("SELECT id FROM users WHERE role = 'admin' AND active = 1 FOR UPDATE"))
        others = conn.execute(
            text("SELECT COUNT(*) FROM users WHERE role = 'admin' AND active = 1 AND id <> :id"),
            {"id": user_id},
        ).scalar_one()
        if int(others) == 0:
            raise AdminGuardError(
                "Это последний активный админ: его нельзя разжаловать, отключить или удалить. "
                "Сначала назначьте админом другого участника."
            )


def update_user(
    user_id: int,
    full_name: Optional[str] = None,
    login: Optional[str] = None,
    password: Optional[str] = None,
    active: Optional[bool] = None,
    role: Optional[str] = None,
    acting_user_id: Optional[int] = None,
    db_path: DbTarget = None,
) -> None:
    """Raises DuplicateError if the new login is taken, AdminGuardError if the change
    would demote yourself or leave no active admin."""
    from auth import hash_password

    if role is not None and role not in ("admin", "member"):
        raise ValueError(f"Недопустимая роль: {role}")
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
    if role is not None:
        fields.append("role = :role")
        params["role"] = role
    if not fields:
        return
    with get_engine(db_path).begin() as conn:
        _check_admin_guard(conn, user_id, acting_user_id, role, active)
        conn.execute(text(f"UPDATE users SET {', '.join(fields)} WHERE id = :id"), params)


def delete_user(user_id: int, acting_user_id: Optional[int] = None,
                db_path: DbTarget = None) -> None:
    """Delete user (participations cascade) and events left without participants.
    Raises AdminGuardError for yourself or the last active admin."""
    with get_engine(db_path).begin() as conn:
        _check_admin_guard(conn, user_id, acting_user_id, None, None, deleting=True)
        conn.execute(text("DELETE FROM users WHERE id = :id"), {"id": user_id})
        _delete_orphans(conn)


# ── Events & participations ─────────────────────────────────────────────────



# ── Import members from Excel ───────────────────────────────────────────────

IMPORT_COLUMNS = {"фио": "full_name", "логин": "login", "пароль": "password"}
LOGIN_RE = re.compile(r"^[A-Za-z0-9._@-]{1,64}$")
ST_CREATE = "будет создан"
ST_EXISTS = "логин уже есть — пропуск"
ST_EMPTY = "ошибка: пустое поле"
ST_DUP = "ошибка: дубль в файле"
ST_BAD_LOGIN = "ошибка: недопустимый логин"


def _cell_str(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        value = int(value)  # 12345.0 → "12345" (numeric passwords/logins)
    return str(value).strip()


def parse_members_xlsx(data: bytes, db_path: DbTarget = None) -> list[dict]:
    """Rows of an .xlsx with headers ФИО / Логин / Пароль (row 1, any order/case).

    Returns [{row, full_name, login, password, status, ok}], nothing is written.
    Raises ValueError (Russian message) if the file or headers are wrong.
    """
    import io

    import openpyxl

    try:
        wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"Не удалось прочитать файл Excel: {exc.__class__.__name__}") from exc
    try:
        rows = list(wb.worksheets[0].iter_rows(values_only=True))
    finally:
        wb.close()
    if not rows:
        raise ValueError("Файл пустой.")
    header = [_cell_str(h).lower() for h in rows[0]]
    idx = {}
    for i, h in enumerate(header):
        if h in IMPORT_COLUMNS and IMPORT_COLUMNS[h] not in idx:
            idx[IMPORT_COLUMNS[h]] = i
    missing = [name for name, field in (("ФИО", "full_name"), ("Логин", "login"),
                                        ("Пароль", "password")) if field not in idx]
    if missing:
        raise ValueError("В первой строке нет столбцов: " + ", ".join(missing)
                         + ". Нужны заголовки ФИО, Логин, Пароль.")
    with get_engine(db_path).connect() as conn:
        existing = set(conn.execute(text("SELECT login FROM users")).scalars().all())
    out: list[dict] = []
    seen: set[str] = set()
    for n, r in enumerate(rows[1:], start=2):
        vals = {f: _cell_str(r[i] if i < len(r) else None) for f, i in idx.items()}
        if not any(vals.values()):
            continue  # blank row
        login = vals["login"]
        if not all(vals.values()):
            status = ST_EMPTY
        elif not LOGIN_RE.match(login):
            status = ST_BAD_LOGIN
        elif login in seen:
            status = ST_DUP
        elif login in existing:
            status = ST_EXISTS
        else:
            status = ST_CREATE
        if login:
            seen.add(login)
        out.append({"row": n, **vals, "status": status, "ok": status == ST_CREATE})
    return out


def import_members(rows: list[dict], db_path: DbTarget = None) -> tuple[int, int]:
    """Create role=member users for rows marked ok. Existing logins are never
    touched. Returns (created, skipped)."""
    created = skipped = 0
    for r in rows:
        if not r.get("ok"):
            skipped += 1
            continue
        if get_user_by_login(r["login"], db_path=db_path) is not None:
            skipped += 1
            continue
        try:
            create_user(r["login"], r["password"], r["full_name"], "member", db_path=db_path)
            created += 1
        except DuplicateError:  # created concurrently
            skipped += 1
    return created, skipped


def normalize_achievement(value: Optional[str]) -> Optional[str]:
    """Номер достижения: обычная строка, strip, не длиннее 64 символов; пусто → NULL."""
    value = (value or "").strip()
    if not value:
        return None
    if len(value) > ACHIEVEMENT_MAX_LEN:
        raise ValueError(f"Номер достижения не длиннее {ACHIEVEMENT_MAX_LEN} символов")
    return value


def _event_extras(event_type: str, article_topic: Optional[str], indexing: Optional[str]):
    if event_type != ARTICLE_TYPE:
        return None, None
    topic = (article_topic or "").strip() or None
    idx = (indexing or "").strip() or None
    if idx is not None and idx not in INDEXING_OPTIONS:
        raise ValueError(f"Недопустимая индексация: {idx}")
    return topic, idx


def get_or_create_event_ex(
    title: str,
    event_type: str,
    event_date: str | date,
    article_topic: Optional[str] = None,
    indexing: Optional[str] = None,
    db_path: DbTarget = None,
) -> dict:
    """Find/create event (dedup by title_norm + type + date).

    Returns {'event_id', 'created', 'indexing_conflict'}; indexing_conflict is the
    already stored indexing when a co-author passes a different one (first wins).
    Missing article_topic/indexing on an existing event are filled in.
    """
    if event_type not in EVENT_TYPES:
        raise ValueError(f"Недопустимый тип: {event_type}")
    title = title.strip()
    title_norm = normalize_title(title)
    if not title_norm:
        raise ValueError("Название мероприятия пустое")
    topic, idx = _event_extras(event_type, article_topic, indexing)
    if isinstance(event_date, date):
        event_date = event_date.isoformat()
    key = {"tn": title_norm, "t": event_type, "d": event_date}
    select_sql = text(
        "SELECT id, article_topic, indexing FROM events "
        "WHERE title_norm = :tn AND type = :t AND event_date = :d"
    )
    eng = get_engine(db_path)

    def _existing(row) -> dict:  # noqa: ANN001
        eid, old_topic, old_idx = int(row[0]), row[1], row[2]
        conflict = old_idx if (idx and old_idx and old_idx != idx) else None
        fill = {}
        if topic and not old_topic:
            fill["article_topic"] = topic
        if idx and not old_idx:
            fill["indexing"] = idx
        if fill:
            with eng.begin() as conn:
                conn.execute(
                    text("UPDATE events SET " + ", ".join(f"{k} = :{k}" for k in fill)
                         + " WHERE id = :id"),
                    {**fill, "id": eid},
                )
        return {"event_id": eid, "created": False, "indexing_conflict": conflict}

    with eng.connect() as conn:
        row = conn.execute(select_sql, key).first()
    if row:
        return _existing(row)
    try:
        with eng.begin() as conn:
            eid = _insert_returning_id(
                conn,
                "INSERT INTO events (title, title_norm, type, event_date, created_at, "
                "article_topic, indexing) VALUES (:title, :tn, :t, :d, :now, :topic, :idx)",
                {**key, "title": title, "now": _now(), "topic": topic, "idx": idx},
            )
        return {"event_id": eid, "created": True, "indexing_conflict": None}
    except IntegrityError:
        # Created concurrently by another session → reuse it.
        with eng.connect() as conn:
            row = conn.execute(select_sql, key).first()
        if row:
            return _existing(row)
        raise


def get_or_create_event(
    title: str,
    event_type: str,
    event_date: str | date,
    db_path: DbTarget = None,
) -> int:
    """Return event id; create if missing. Dedup by title_norm + type + date."""
    return get_or_create_event_ex(title, event_type, event_date, db_path=db_path)["event_id"]


def add_participation_ex(
    user_id: int,
    title: str,
    event_type: str,
    event_date: str | date,
    article_topic: Optional[str] = None,
    indexing: Optional[str] = None,
    achievement_number: Optional[str] = None,
    db_path: DbTarget = None,
) -> dict:
    """Like add_participation, returns {'participation_id', 'event_id', 'indexing_conflict'}."""
    ach = normalize_achievement(achievement_number)  # validate before touching the DB
    ev = get_or_create_event_ex(
        title, event_type, event_date, article_topic, indexing, db_path=db_path
    )
    try:
        pid = _insert_participation(user_id, ev["event_id"], ach, db_path)
    except IntegrityError:
        # The event may have just been removed as an orphan by another session → retry once.
        with get_engine(db_path).connect() as conn:
            alive = conn.execute(text("SELECT 1 FROM events WHERE id = :e"),
                                 {"e": ev["event_id"]}).first()
        if alive:
            raise
        ev = get_or_create_event_ex(
            title, event_type, event_date, article_topic, indexing, db_path=db_path
        )
        pid = _insert_participation(user_id, ev["event_id"], ach, db_path)
    return {"participation_id": pid, "event_id": ev["event_id"],
            "indexing_conflict": ev["indexing_conflict"]}


def _insert_participation(user_id: int, event_id: int, ach: Optional[str],
                          db_path: DbTarget) -> int:
    with get_engine(db_path).begin() as conn:
        return _insert_returning_id(
            conn,
            "INSERT INTO participations (user_id, event_id, created_at, achievement_number) "
            "VALUES (:u, :e, :now, :ach)",
            {"u": user_id, "e": event_id, "now": _now(), "ach": ach},
        )


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
    r = add_participation_ex(user_id, title, event_type, event_date, db_path=db_path)
    return r["participation_id"], r["event_id"]


def set_achievement_number(
    participation_id: int,
    number: Optional[str],
    user_id: Optional[int] = None,
    db_path: DbTarget = None,
) -> bool:
    """Set/clear achievement number. If user_id given, only that user's row."""
    params: dict[str, Any] = {"id": participation_id, "n": normalize_achievement(number)}
    sql = "UPDATE participations SET achievement_number = :n WHERE id = :id"
    if user_id is not None:
        sql += " AND user_id = :u"
        params["u"] = user_id
    with get_engine(db_path).begin() as conn:
        return conn.execute(text(sql), params).rowcount > 0


def delete_participation(
    participation_id: int,
    user_id: Optional[int] = None,
    db_path: DbTarget = None,
) -> bool:
    """Delete participation. If user_id given, only that user's row. Returns True if deleted.
    The event is deleted too when this was its last participation."""
    with get_engine(db_path).begin() as conn:
        eid = conn.execute(
            text("SELECT event_id FROM participations WHERE id = :id"), {"id": participation_id}
        ).scalar()
        if user_id is not None:
            res = conn.execute(
                text("DELETE FROM participations WHERE id = :id AND user_id = :u"),
                {"id": participation_id, "u": user_id},
            )
        else:
            res = conn.execute(
                text("DELETE FROM participations WHERE id = :id"), {"id": participation_id}
            )
        if res.rowcount > 0 and eid is not None:
            _delete_orphans(conn, [int(eid)])
        return res.rowcount > 0


def _delete_orphans(conn, event_ids: Optional[list[int]] = None) -> int:  # noqa: ANN001
    """Delete events without participations (all, or only among event_ids).
    Linked meetings stay in the report: their event_id is cleared first (same as the
    FK's ON DELETE SET NULL, done explicitly so it never depends on FK support)."""
    cond = "NOT EXISTS (SELECT 1 FROM participations p WHERE p.event_id = events.id)"
    params: dict[str, Any] = {}
    if event_ids is not None:
        if not event_ids:
            return 0
        names = [f"e{i}" for i in range(len(event_ids))]
        cond += " AND id IN (" + ", ".join(":" + n for n in names) + ")"
        params = dict(zip(names, event_ids))
    orphan_sel = f"SELECT id FROM events WHERE {cond}"
    conn.execute(text(f"UPDATE meetings SET event_id = NULL WHERE event_id IN ({orphan_sel})"), params)
    return conn.execute(text(f"DELETE FROM events WHERE {cond}"), params).rowcount


def delete_orphan_events(db_path: DbTarget = None) -> int:  # also accepts an Engine
    """Delete all events with 0 participations; returns how many were removed."""
    eng = db_path if isinstance(db_path, Engine) else get_engine(db_path)
    with eng.begin() as conn:
        return _delete_orphans(conn)


def update_event(
    event_id: int,
    title: str,
    event_type: str,
    event_date: str | date,
    article_topic: Optional[str] = None,
    indexing: Optional[str] = None,
    db_path: DbTarget = None,
) -> dict:
    """Admin edit of a shared event (affects all co-participants).

    If the new title/type/date equals another existing event, the two are merged:
    participations move to that event (a member already in it keeps one row, the
    achievement number is kept if the remaining row had none) and this event is
    removed. Returns {'event_id', 'merged'}.
    """
    if event_type not in EVENT_TYPES:
        raise ValueError(f"Недопустимый тип: {event_type}")
    title = (title or "").strip()
    title_norm = normalize_title(title)
    if not title_norm:
        raise ValueError("Название мероприятия пустое")
    topic, idx = _event_extras(event_type, article_topic, indexing)
    d = _to_date(event_date).isoformat()
    with get_engine(db_path).begin() as conn:
        target = conn.execute(
            text("SELECT id FROM events WHERE title_norm = :tn AND type = :t "
                 "AND event_date = :d AND id <> :id"),
            {"tn": title_norm, "t": event_type, "d": d, "id": event_id},
        ).scalar()
        if target is None:
            res = conn.execute(
                text("UPDATE events SET title = :title, title_norm = :tn, type = :t, "
                     "event_date = :d, article_topic = :topic, indexing = :idx WHERE id = :id"),
                {"title": title, "tn": title_norm, "t": event_type, "d": d,
                 "topic": topic, "idx": idx, "id": event_id},
            )
            if res.rowcount == 0:
                raise ValueError("Мероприятие не найдено (возможно, уже удалено).")
            return {"event_id": event_id, "merged": False}
        target = int(target)
        # members already in the target: keep their row, copy a missing number over
        conn.execute(text(
            "UPDATE participations SET achievement_number = ("
            "  SELECT o.achievement_number FROM participations o"
            "  WHERE o.event_id = :src AND o.user_id = participations.user_id) "
            "WHERE event_id = :dst AND (achievement_number IS NULL OR achievement_number = '') "
            "AND user_id IN (SELECT user_id FROM participations WHERE event_id = :src)"
        ), {"src": event_id, "dst": target})
        conn.execute(text(
            "DELETE FROM participations WHERE event_id = :src "
            "AND user_id IN (SELECT user_id FROM participations WHERE event_id = :dst)"
        ), {"src": event_id, "dst": target})
        conn.execute(text("UPDATE participations SET event_id = :dst WHERE event_id = :src"),
                     {"src": event_id, "dst": target})
        conn.execute(text("UPDATE meetings SET event_id = :dst WHERE event_id = :src"),
                     {"src": event_id, "dst": target})
        if topic or idx:
            conn.execute(text(
                "UPDATE events SET article_topic = COALESCE(article_topic, :topic), "
                "indexing = COALESCE(indexing, :idx) WHERE id = :dst"
            ), {"topic": topic, "idx": idx, "dst": target})
        conn.execute(text("DELETE FROM events WHERE id = :src"), {"src": event_id})
        return {"event_id": target, "merged": True}


def event_participants_count(event_id: int, db_path: DbTarget = None) -> int:
    with get_engine(db_path).connect() as conn:
        return int(conn.execute(
            text("SELECT COUNT(*) FROM participations WHERE event_id = :e"), {"e": event_id}
        ).scalar_one())


def list_participations_for_user(user_id: int, db_path: DbTarget = None) -> list[dict]:
    with get_engine(db_path).connect() as conn:
        return _rows(conn.execute(
            text(
                """
                SELECT p.id AS participation_id, p.created_at AS joined_at,
                       p.achievement_number,
                       e.id AS event_id, e.title, e.type, e.event_date,
                       e.article_topic, e.indexing
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
    without_achievement: bool = False,
    db_path: DbTarget = None,
) -> list[dict]:
    sql = """
        SELECT p.id AS participation_id, p.created_at AS joined_at,
               p.achievement_number,
               u.id AS user_id, u.full_name, u.login,
               e.id AS event_id, e.title, e.type, e.event_date,
               e.article_topic, e.indexing
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
    if without_achievement:
        sql += " AND (p.achievement_number IS NULL OR p.achievement_number = '')"
    sql += " ORDER BY e.event_date DESC, LOWER(u.full_name)"
    with get_engine(db_path).connect() as conn:
        return _rows(conn.execute(text(sql), params))


def year_range(year: Optional[int]) -> tuple[Optional[str], Optional[str]]:
    """ISO date bounds for a year (portable: no dialect-specific date functions)."""
    if not year:
        return None, None
    return f"{int(year):04d}-01-01", f"{int(year):04d}-12-31"


def _event_filter(year: Optional[int], alias: str = "e") -> tuple[str, dict]:
    df, dt = year_range(year)
    if df is None:
        return "", {}
    return f" AND {alias}.event_date >= :df AND {alias}.event_date <= :dt", {"df": df, "dt": dt}


def stats_by_person(year: Optional[int] = None, db_path: DbTarget = None) -> list[dict]:
    cond, params = _event_filter(year)
    with get_engine(db_path).connect() as conn:
        rows = _rows(conn.execute(text(
            f"""
            SELECT u.id AS user_id, u.full_name, u.login,
                   COUNT(x.pid) AS total,
                   SUM(CASE WHEN x.type = 'грант' THEN 1 ELSE 0 END) AS grants,
                   SUM(CASE WHEN x.type = 'конференция' THEN 1 ELSE 0 END) AS conferences,
                   SUM(CASE WHEN x.type = 'конкурс' THEN 1 ELSE 0 END) AS contests,
                   SUM(CASE WHEN x.type = 'стипендия' THEN 1 ELSE 0 END) AS scholarships,
                   SUM(CASE WHEN x.type = 'статья' THEN 1 ELSE 0 END) AS articles,
                   SUM(CASE WHEN x.ach IS NOT NULL AND x.ach <> '' THEN 1 ELSE 0 END) AS with_number
            FROM users u
            LEFT JOIN (
                SELECT p.id AS pid, p.user_id, e.type, p.achievement_number AS ach
                FROM participations p
                JOIN events e ON e.id = p.event_id
                WHERE 1=1 {cond}
            ) x ON x.user_id = u.id
            WHERE u.role = 'member' AND u.active = 1
            GROUP BY u.id, u.full_name, u.login
            ORDER BY COUNT(x.pid) DESC, LOWER(u.full_name)
            """
        ), params))
    for r in rows:  # Postgres SUM → bigint/None; normalize to int
        for k in ("total", "grants", "conferences", "contests", "scholarships", "articles",
                  "with_number"):
            r[k] = int(r[k] or 0)
    return rows


def stats_by_type(year: Optional[int] = None, db_path: DbTarget = None) -> list[dict]:
    cond, params = _event_filter(year)
    with get_engine(db_path).connect() as conn:
        rows = _rows(conn.execute(text(
            f"""
            SELECT e.type, COUNT(DISTINCT e.id) AS events_count,
                   COUNT(p.id) AS participations_count
            FROM events e
            JOIN participations p ON p.event_id = e.id
            WHERE 1=1 {cond}
            GROUP BY e.type
            ORDER BY e.type
            """
        ), params))
    for r in rows:
        r["events_count"] = int(r["events_count"] or 0)
        r["participations_count"] = int(r["participations_count"] or 0)
    return rows


def stats_by_event(year: Optional[int] = None, db_path: DbTarget = None) -> list[dict]:
    cond, params = _event_filter(year)
    with get_engine(db_path).connect() as conn:
        rows = _rows(conn.execute(text(
            f"""
            SELECT e.id AS event_id, e.title, e.type, e.event_date,
                   COUNT(p.id) AS participants_count
            FROM events e
            JOIN participations p ON p.event_id = e.id
            WHERE 1=1 {cond}
            GROUP BY e.id, e.title, e.type, e.event_date
            ORDER BY e.event_date DESC, COUNT(p.id) DESC
            """
        ), params))
    for r in rows:
        r["participants_count"] = int(r["participants_count"] or 0)
    return rows


def stats_articles_by_indexing(year: Optional[int] = None, db_path: DbTarget = None) -> list[dict]:
    """Статьи по индексации: [{'indexing', 'articles', 'authors'}] (без индексации → 'не указана')."""
    cond, params = _event_filter(year)
    with get_engine(db_path).connect() as conn:
        rows = _rows(conn.execute(text(
            f"""
            SELECT e.indexing, COUNT(DISTINCT e.id) AS articles, COUNT(p.id) AS authors
            FROM events e
            JOIN participations p ON p.event_id = e.id
            WHERE e.type = 'статья' {cond}
            GROUP BY e.indexing
            """
        ), params))
    order = {k: i for i, k in enumerate(INDEXING_OPTIONS)}
    out = [
        {"indexing": r["indexing"] or "не указана",
         "articles": int(r["articles"] or 0), "authors": int(r["authors"] or 0)}
        for r in rows
    ]
    return sorted(out, key=lambda r: order.get(r["indexing"], len(order)))


def count_events(year: Optional[int] = None, db_path: DbTarget = None) -> int:
    cond, params = _event_filter(year)
    with get_engine(db_path).connect() as conn:
        return int(conn.execute(
            text(f"SELECT COUNT(*) FROM events e WHERE EXISTS "
                 f"(SELECT 1 FROM participations p WHERE p.event_id = e.id) {cond}"), params
        ).scalar_one())


def count_participations(year: Optional[int] = None, db_path: DbTarget = None) -> int:
    cond, params = _event_filter(year)
    with get_engine(db_path).connect() as conn:
        return int(conn.execute(text(
            f"SELECT COUNT(*) FROM participations p JOIN events e ON e.id = p.event_id "
            f"WHERE 1=1 {cond}"
        ), params).scalar_one())


def participations_by_month(year: Optional[int] = None, db_path: DbTarget = None) -> list[int]:
    """12 counts (Jan..Dec). Month grouping is done in Python → portable."""
    cond, params = _event_filter(year)
    with get_engine(db_path).connect() as conn:
        dates = conn.execute(text(
            f"SELECT e.event_date FROM participations p JOIN events e ON e.id = p.event_id "
            f"WHERE 1=1 {cond}"
        ), params).scalars().all()
    counts = [0] * 12
    for d in dates:
        try:
            counts[_to_date(d).month - 1] += 1
        except (TypeError, ValueError):
            continue
    return counts


def user_year_summary(user_id: int, year: int, db_path: DbTarget = None) -> dict:
    """{'total': N, 'грант': x, 'конференция': y, 'конкурс': z} for one member and year."""
    cond, params = _event_filter(year)
    with get_engine(db_path).connect() as conn:
        rows = conn.execute(text(
            f"SELECT e.type, COUNT(*) AS n FROM participations p "
            f"JOIN events e ON e.id = p.event_id WHERE p.user_id = :u {cond} GROUP BY e.type"
        ), {"u": user_id, **params}).all()
    out = {t: 0 for t in EVENT_TYPES}
    for t, n in rows:
        out[t] = int(n)
    out["total"] = sum(out[t] for t in EVENT_TYPES)
    return out


def available_years(db_path: DbTarget = None) -> list[int]:
    """Years that have events or meetings (desc)."""
    years: set[int] = set()
    with get_engine(db_path).connect() as conn:
        for d in conn.execute(text("SELECT DISTINCT event_date FROM events")).scalars():
            try:
                years.add(_to_date(d).year)
            except (TypeError, ValueError):
                pass
        for d in conn.execute(text("SELECT DISTINCT meeting_date FROM meetings")).scalars():
            try:
                years.add(_to_date(d).year)
            except (TypeError, ValueError):
                pass
    return sorted(years, reverse=True)


# ── Meetings (заседания СНО) ────────────────────────────────────────────────


def _to_date(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


_TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


def normalize_time(value: Optional[str]) -> str:
    """'9:5'-like input is rejected; '9:30' → '09:30'; empty → default 13:30."""
    value = (value or "").strip().replace(".", ":")
    if not value:
        return DEFAULT_MEETING_TIME
    m = _TIME_RE.match(value)
    if not m:
        raise ValueError("Время должно быть в формате ЧЧ:ММ, например 13:30")
    return f"{int(m.group(1)):02d}:{m.group(2)}"


def join_formats(selected: list[str] | tuple[str, ...], other: str = "") -> str:
    items = [s.strip() for s in selected if s and s.strip()]
    for extra in re.split(r"[,;\n]", other or ""):
        extra = extra.strip()
        if extra and extra not in items:
            items.append(extra)
    return ", ".join(items)


def split_formats(value: str) -> tuple[list[str], str]:
    """Stored format string → (known formats, free-text rest)."""
    known, other = [], []
    for item in (value or "").split(","):
        item = item.strip()
        if not item:
            continue
        (known if item in MEETING_FORMATS else other).append(item)
    return known, ", ".join(other)


def _meeting_row(r: dict) -> dict:
    r["meeting_date"] = _to_date(r["meeting_date"])
    return r


def add_meeting(
    meeting_date: str | date,
    meeting_time: str = DEFAULT_MEETING_TIME,
    location: str = "",
    topic: str = "",
    fmt: str = "",
    kind: str = DEFAULT_MEETING_KIND,
    event_id: Optional[int] = None,
    db_path: DbTarget = None,
) -> int:
    """Add a СНО activity (заседание / конференция / форум / ...) to the report list.
    event_id links it to a member event (so it is not offered again)."""
    d = _to_date(meeting_date).isoformat()
    with get_engine(db_path).begin() as conn:
        return _insert_returning_id(
            conn,
            "INSERT INTO meetings (meeting_date, meeting_time, location, topic, format, "
            "created_at, kind, event_id) VALUES (:d, :t, :loc, :topic, :fmt, :now, :kind, :eid)",
            {"d": d, "t": normalize_time(meeting_time), "loc": (location or "").strip(),
             "topic": (topic or "").strip(), "fmt": (fmt or "").strip(), "now": _now(),
             "kind": _check_kind(kind), "eid": event_id},
        )


def _check_kind(kind: Optional[str]) -> str:
    kind = (kind or DEFAULT_MEETING_KIND).strip()
    if kind not in MEETING_KINDS:
        raise ValueError(f"Недопустимый вид мероприятия: {kind}")
    return kind


def update_meeting(
    meeting_id: int,
    meeting_date: str | date,
    meeting_time: str,
    location: str,
    topic: str,
    fmt: str,
    kind: Optional[str] = None,
    db_path: DbTarget = None,
) -> bool:
    params = {"id": meeting_id, "d": _to_date(meeting_date).isoformat(),
              "t": normalize_time(meeting_time), "loc": (location or "").strip(),
              "topic": (topic or "").strip(), "fmt": (fmt or "").strip()}
    extra = ""
    if kind is not None:
        extra = ", kind = :kind"
        params["kind"] = _check_kind(kind)
    with get_engine(db_path).begin() as conn:
        res = conn.execute(
            text(
                "UPDATE meetings SET meeting_date = :d, meeting_time = :t, location = :loc, "
                f"topic = :topic, format = :fmt{extra} WHERE id = :id"
            ),
            params,
        )
        return res.rowcount > 0


def delete_meeting(meeting_id: int, db_path: DbTarget = None) -> bool:
    with get_engine(db_path).begin() as conn:
        res = conn.execute(text("DELETE FROM meetings WHERE id = :id"), {"id": meeting_id})
        return res.rowcount > 0


def get_meeting(meeting_id: int, db_path: DbTarget = None) -> Optional[dict]:
    with get_engine(db_path).connect() as conn:
        r = _one(conn.execute(text("SELECT * FROM meetings WHERE id = :id"), {"id": meeting_id}))
    return _meeting_row(r) if r else None


def list_meetings(year: Optional[int] = None, db_path: DbTarget = None) -> list[dict]:
    """Meetings sorted by date/time; each row gets 'number' = 1..N within the list."""
    df, dt = year_range(year)
    sql = "SELECT * FROM meetings WHERE 1=1"
    params: dict[str, Any] = {}
    if df:
        sql += " AND meeting_date >= :df AND meeting_date <= :dt"
        params = {"df": df, "dt": dt}
    sql += " ORDER BY meeting_date, meeting_time, id"
    with get_engine(db_path).connect() as conn:
        rows = [_meeting_row(r) for r in _rows(conn.execute(text(sql), params))]
    for i, r in enumerate(rows, start=1):
        r["number"] = i
    return rows


def count_meetings(
    year: Optional[int] = None,
    kind: Optional[str] = None,
    exclude_kind: Optional[str] = None,
    db_path: DbTarget = None,
) -> int:
    rows = list_meetings(year, db_path=db_path)
    if kind is not None:
        rows = [r for r in rows if r["kind"] == kind]
    if exclude_kind is not None:
        rows = [r for r in rows if r["kind"] != exclude_kind]
    return len(rows)


EVENT_TYPE_TO_KIND = {"конференция": "Конференция"}


def unlinked_member_events(year: Optional[int] = None, db_path: DbTarget = None) -> list[dict]:
    """Member events (deduplicated by the events table) with ≥1 participation in the year
    that are not linked to any meeting yet. Suggested kind/topic/format included."""
    cond, params = _event_filter(year)
    with get_engine(db_path).connect() as conn:
        rows = _rows(conn.execute(text(
            f"""
            SELECT e.id AS event_id, e.title, e.type, e.event_date,
                   COUNT(p.id) AS participants_count
            FROM events e
            JOIN participations p ON p.event_id = e.id
            WHERE NOT EXISTS (SELECT 1 FROM meetings m WHERE m.event_id = e.id) {cond}
            GROUP BY e.id, e.title, e.type, e.event_date
            ORDER BY e.event_date, LOWER(e.title)
            """
        ), params))
    for r in rows:
        r["participants_count"] = int(r["participants_count"] or 0)
        r["suggested_kind"] = EVENT_TYPE_TO_KIND.get(r["type"], "Другое")
        r["suggested_topic"] = f"Выступление членов СНО в рамках «{r['title']}»"
        r["suggested_formats"] = ["Выступления с докладами"] if r["type"] == "конференция" else []
    return rows


def recent_locations(limit: int = 10, db_path: DbTarget = None) -> list[str]:
    """Distinct locations, most recently used first."""
    with get_engine(db_path).connect() as conn:
        rows = conn.execute(text(
            "SELECT location FROM meetings WHERE location <> '' "
            "ORDER BY meeting_date DESC, id DESC"
        )).scalars().all()
    seen: list[str] = []
    for loc in rows:
        if loc not in seen:
            seen.append(loc)
        if len(seen) >= limit:
            break
    return seen


# ── App settings (key/value) ────────────────────────────────────────────────


def get_app_setting(key: str, default: Optional[str] = None, db_path: DbTarget = None) -> Optional[str]:
    with get_engine(db_path).connect() as conn:
        v = conn.execute(text("SELECT value FROM settings WHERE key = :k"), {"k": key}).scalar()
    return default if v is None else v


def set_app_setting(key: str, value: str, db_path: DbTarget = None) -> None:
    # ON CONFLICT ... DO UPDATE works in both SQLite (3.24+) and Postgres.
    with get_engine(db_path).begin() as conn:
        conn.execute(
            text(
                "INSERT INTO settings (key, value) VALUES (:k, :v) "
                "ON CONFLICT (key) DO UPDATE SET value = excluded.value"
            ),
            {"k": key, "v": value},
        )


def _clean_signatories(items: Any) -> list[dict]:
    out = []
    for it in items or []:
        if not isinstance(it, dict):
            continue
        pos = str(it.get("position") or "").strip()
        name = str(it.get("name") or "").strip()
        if pos or name:
            out.append({"position": pos, "name": name})
    return out


def get_report_settings(db_path: DbTarget = None) -> dict:
    raw = get_app_setting("signatories", None, db_path=db_path)
    if raw is None:
        signatories = [dict(s) for s in DEFAULT_SIGNATORIES]
    else:
        try:
            signatories = _clean_signatories(json.loads(raw))
        except (ValueError, TypeError):
            signatories = [dict(s) for s in DEFAULT_SIGNATORIES]
    return {
        "sno_name": get_app_setting("sno_name", DEFAULT_SNO_NAME, db_path=db_path),
        "appendix_label": get_app_setting("appendix_label", DEFAULT_APPENDIX_LABEL, db_path=db_path),
        "signatories": signatories,
    }


def save_report_settings(
    sno_name: str,
    appendix_label: str,
    signatories: list[dict],
    db_path: DbTarget = None,
) -> None:
    name = (sno_name or "").strip().strip("«»\"").strip()
    set_app_setting("sno_name", name or DEFAULT_SNO_NAME, db_path=db_path)
    set_app_setting("appendix_label", (appendix_label or "").strip(), db_path=db_path)
    set_app_setting(
        "signatories",
        json.dumps(_clean_signatories(signatories), ensure_ascii=False),
        db_path=db_path,
    )
