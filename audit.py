"""Журнал действий (audit log) СНО ДГТУ.

Таблица audit_log создаётся в db._SCHEMA тем же механизмом, что и остальные
(CREATE TABLE IF NOT EXISTS: SQLite и Postgres, старые данные не затрагиваются).

Кто: actor_* - реальный пользователь; as_user_* - «от имени» кого он действовал
(админ в режиме «войти как участник»); target_user_* - чья запись изменена.
Время ts - московское (UTC+3, без перехода на летнее время), ISO-текст: сортируется
и фильтруется как строка. В журнал НИКОГДА не пишутся пароли и хэши паролей.

log() не бросает исключений: сбой журнала не должен ломать основную операцию.
"""

from __future__ import annotations

import json
import sys
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable, Optional

from sqlalchemy import text
from sqlalchemy.engine import Engine

import db

MSK = timezone(timedelta(hours=3))
SUMMARY_MAX = 900
VALUE_MAX = 120      # длина значения в кратком описании
DETAIL_VALUE_MAX = 500  # длина значения в details (JSON)
LIST_LIMIT = 5000

# Действия: код -> подпись в интерфейсе
ACTION_LABELS = {
    "achievement_create": "Достижение: создание",
    "achievement_update": "Достижение: изменение",
    "achievement_delete": "Достижение: удаление",
    "login": "Вход",
    "impersonate_start": "Вход от имени участника",
    "impersonate_stop": "Возврат в админа",
    "import_members": "Импорт участников (Excel)",
    "settings_change": "Смена настроек",
    "user_create": "Участник: создание",
    "user_update": "Участник: изменение",
    "user_delete": "Участник: удаление",
    "meeting_create": "Заседание: создание",
    "meeting_update": "Заседание: изменение",
    "meeting_delete": "Заседание: удаление",
    "events_merge": "Мероприятия: объединение дублей",
    "events_not_dup": "Мероприятия: «не дубль»",
    "events_not_dup_undo": "Мероприятия: «не дубль» отменено",
    "backup_download": "Скачивание бэкапа",
    "backup_restore": "Восстановление из бэкапа",
}


def action_label(code: str) -> str:
    return ACTION_LABELS.get(code, code)


def now_msk() -> str:
    return datetime.now(MSK).replace(tzinfo=None).isoformat(timespec="seconds")


def _eng(db_path: Any) -> Engine:
    return db_path if isinstance(db_path, Engine) else db.get_engine(db_path)


def _short(value: Any, limit: int = VALUE_MAX) -> str:
    s = "" if value is None else str(value)
    s = " ".join(s.split())
    return s if len(s) <= limit else s[: limit - 3] + "..."


def show(value: Any) -> str:
    """Значение для краткого описания: «текст» или (пусто)."""
    if value is None or str(value).strip() == "":
        return "(пусто)"
    return f"«{_short(value)}»"


def _person(u: Optional[dict]) -> tuple[Optional[int], Optional[str], Optional[str]]:
    if not u:
        return None, None, None
    uid = u.get("id")
    return (int(uid) if uid not in (None, "") else None,
            u.get("login"), u.get("full_name") or u.get("name"))


def who(session: Any) -> dict:
    """actor / as_user из st.session_state (или любого mapping): при «войти как» actor - реальный
    админ (_imp_admin), as_user - участник, под которым он работает."""
    user = session.get("user") if hasattr(session, "get") else None
    imp = session.get("_imp_admin") if hasattr(session, "get") else None
    if imp:
        return {"actor": imp, "as_user": user}
    return {"actor": user, "as_user": None}


def _clip(snapshot: dict) -> dict:
    return {k: _short(v, DETAIL_VALUE_MAX) for k, v in snapshot.items() if v not in (None, "")}


def diff_fields(before: dict, after: dict) -> list[dict]:
    """[{field, before, after}] для ключей, значения которых различаются (сравнение как строк)."""
    out = []
    for key in list(before) + [k for k in after if k not in before]:
        b, a = before.get(key), after.get(key)
        if ("" if b is None else str(b)) != ("" if a is None else str(a)):
            out.append({"field": key,
                        "before": _short(b, DETAIL_VALUE_MAX), "after": _short(a, DETAIL_VALUE_MAX)})
    return out


def describe_changes(changes: Iterable[dict]) -> str:
    """«Поле: было «x», стало «y»; ...»"""
    return "; ".join(f"{c['field']}: было {show(c['before'])}, стало {show(c['after'])}" for c in changes)


def log(action: str, actor: Optional[dict] = None, as_user: Optional[dict] = None,
        target_user: Optional[dict] = None, entity: Optional[str] = None,
        entity_id: Optional[int] = None, summary: str = "", details: Optional[dict] = None,
        db_path: Any = None) -> Optional[int]:
    """Записать действие. Возвращает id строки или None при сбое (исключение не пробрасывается)."""
    try:
        a_id, a_login, a_name = _person(actor)
        s_id, _s_login, s_name = _person(as_user)
        t_id, _t_login, t_name = _person(target_user)
        summary = " ".join(str(summary or "").split())
        if len(summary) > SUMMARY_MAX:
            summary = summary[: SUMMARY_MAX - 3] + "..."
        params = {
            "ts": now_msk(), "a_id": a_id, "a_login": a_login, "a_name": a_name,
            "s_id": s_id, "s_name": s_name, "t_id": t_id, "t_name": t_name,
            "action": action, "entity": entity, "eid": int(entity_id) if entity_id else None,
            "summary": summary, "details": json.dumps(details or {}, ensure_ascii=False, default=str),
        }
        with _eng(db_path).begin() as conn:
            conn.execute(text(
                "INSERT INTO audit_log (ts, actor_id, actor_login, actor_name, as_user_id, as_user_name, "
                "target_user_id, target_user_name, action, entity, entity_id, summary, details) "
                "VALUES (:ts, :a_id, :a_login, :a_name, :s_id, :s_name, :t_id, :t_name, :action, "
                ":entity, :eid, :summary, :details)"), params)
        return 1
    except Exception as exc:  # noqa: BLE001 - журнал не должен ломать основную операцию
        print(f"audit.log failed ({action}): {exc.__class__.__name__}: {exc}", file=sys.stderr)
        return None


def log_ui(session: Any, action: str, **kw: Any) -> Optional[int]:
    """log() с actor/as_user из session_state (режим «от имени участника» учитывается)."""
    return log(action, **who(session), **kw)


def _day_bounds(date_from: Optional[date], date_to: Optional[date]) -> tuple[Optional[str], Optional[str]]:
    lo = date_from.isoformat() if date_from else None
    hi = (date_to + timedelta(days=1)).isoformat() if date_to else None
    return lo, hi


@db.cached
def list_entries(user_id: Optional[int] = None, actions: Optional[list[str]] = None,
                 date_from: Optional[date] = None, date_to: Optional[date] = None,
                 search: str = "", limit: int = LIST_LIMIT, db_path: Any = None) -> list[dict]:
    """Новые записи первыми. user_id - участник в любой роли (кто действовал, от имени кого,
    чья запись). Поиск - по всем текстовым полям без учёта регистра (в Python: LOWER() в SQLite
    не понимает кириллицу). Не кэшируется."""
    sql = "SELECT * FROM audit_log WHERE 1=1"
    params: dict[str, Any] = {}
    if user_id:
        sql += " AND (actor_id = :u OR as_user_id = :u OR target_user_id = :u)"
        params["u"] = int(user_id)
    if actions:
        names = [f"a{i}" for i in range(len(actions))]
        sql += " AND action IN (" + ", ".join(":" + n for n in names) + ")"
        params.update(dict(zip(names, actions)))
    lo, hi = _day_bounds(date_from, date_to)
    if lo:
        sql += " AND ts >= :lo"
        params["lo"] = lo
    if hi:
        sql += " AND ts < :hi"
        params["hi"] = hi
    q = " ".join((search or "").casefold().split())
    sql += " ORDER BY id DESC"
    if not q:
        sql += " LIMIT :lim"
        params["lim"] = int(limit)
    with _eng(db_path).connect() as conn:
        rows = [dict(r._mapping) for r in conn.execute(text(sql), params)]
    for r in rows:
        try:
            r["details"] = json.loads(r["details"] or "{}")
        except (TypeError, ValueError):
            r["details"] = {}
    if q:
        def hay(r: dict) -> str:
            parts = [r["summary"], r["actor_name"], r["actor_login"], r["as_user_name"], r["target_user_name"],
                     action_label(r["action"]), r["action"], r["entity"], r["entity_id"], r["ts"]]
            return " ".join(str(p) for p in parts if p not in (None, "")).casefold()
        rows = [r for r in rows if q in hay(r)][: int(limit)]
    return rows


def count_entries(db_path: Any = None) -> int:
    with _eng(db_path).connect() as conn:
        return int(conn.execute(text("SELECT COUNT(*) FROM audit_log")).scalar_one())


@db.cached
def known_users(db_path: Any = None) -> list[tuple[int, str]]:
    """Участники, встречающиеся в журнале (в т.ч. удалённые позже): [(id, имя)]."""
    out: dict[int, str] = {}
    with _eng(db_path).connect() as conn:
        for idc, namec in (("actor_id", "actor_name"), ("as_user_id", "as_user_name"),
                           ("target_user_id", "target_user_name")):
            for uid, name in conn.execute(text(
                    f"SELECT {idc}, MAX({namec}) FROM audit_log WHERE {idc} IS NOT NULL GROUP BY {idc}")):
                out.setdefault(int(uid), name or f"id {uid}")
    return sorted(out.items(), key=lambda kv: kv[1].casefold())
