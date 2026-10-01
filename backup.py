"""Бэкап и восстановление базы СНО (SQLite и Postgres одинаково).

Файл бэкапа - zip:
  backup.json   - полный дамп всех таблиц (из него делается восстановление);
  csv/<таблица>.csv - те же данные по таблицам (UTF-8 с BOM, открываются в Excel);
  README.txt    - что внутри и как восстановить.

ЧУВСТВИТЕЛЬНЫЙ ФАЙЛ: в таблице users лежат хэши паролей (bcrypt) и логины, в остальных
таблицах - личные данные участников. Хранить так же бережно, как пароли, никому не пересылать.
Секрет подписи cookie (settings.cookie_secret) в бэкап НЕ включается: с ним и хэшами из
бэкапа можно было бы подделать вход. После восстановления сайт сам создаст новый ключ
(все «запомнить меня» сессии просто войдут заново).

Восстановление - только скриптом restore_backup.py (с подтверждением), из интерфейса его нет.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import zipfile
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Optional

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine

import audit
import db

FORMAT = "sno-backup"
FORMAT_VERSION = 1

# Порядок: родители раньше детей (внешние ключи). Восстановление удаляет в обратном порядке.
TABLE_ORDER = [
    "users", "events", "participations", "meetings", "settings",
    "indicators", "indicator_rows", "kinds", "kind_subpoints",
    "achievements", "achievement_people", "audit_log",
]
# Ключи settings, которые не попадают в бэкап и не стираются при восстановлении.
EXCLUDED_SETTINGS = ("cookie_secret",)

SENSITIVE_NOTE = ("Файл содержит хэши паролей и личные данные участников - это чувствительный файл. "
                  "Не пересылайте его и храните в закрытом месте.")


def _eng(db_path: Any) -> Engine:
    return db_path if isinstance(db_path, Engine) else db.get_engine(db_path)


def table_names(db_path: Any = None) -> list[str]:
    """Таблицы базы в порядке зависимостей (известные по TABLE_ORDER, затем остальные по алфавиту)."""
    have = set(inspect(_eng(db_path)).get_table_names())
    known = [t for t in TABLE_ORDER if t in have]
    return known + sorted(t for t in have if t not in TABLE_ORDER and not t.startswith(("sqlite_", "events__")))


def _jsonable(v: Any) -> Any:
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, (bytes, memoryview)):
        raise ValueError("Бинарные данные в таблицах не поддерживаются бэкапом")
    return v


def dump(db_path: Any = None) -> dict:
    """Весь дамп в памяти: {"meta": {...}, "tables": {name: {"columns": [...], "rows": [[...], ...]}}}."""
    eng = _eng(db_path)
    tables: dict[str, dict] = {}
    with eng.connect() as conn:
        for name in table_names(eng):
            cols = [c["name"] for c in inspect(eng).get_columns(name)]
            order = "key" if "key" in cols else ("id" if "id" in cols else cols[0])
            res = conn.execute(text(f'SELECT {", ".join(cols)} FROM {name} ORDER BY {order}'))
            rows = [[_jsonable(v) for v in r] for r in res]
            if name == "settings":
                ki = cols.index("key")
                rows = [r for r in rows if r[ki] not in EXCLUDED_SETTINGS]
            tables[name] = {"columns": cols, "rows": rows}
    meta = {
        "format": FORMAT, "format_version": FORMAT_VERSION,
        "created_at": audit.now_msk(), "timezone": "Europe/Moscow (UTC+3)",
        "backend": "Postgres" if eng.dialect.name == "postgresql" else "SQLite",
        "contains_password_hashes": True,
        "excluded": {"settings": list(EXCLUDED_SETTINGS)},
        "row_counts": {t: len(d["rows"]) for t, d in tables.items()},
    }
    return {"meta": meta, "tables": tables}


def _readme(meta: dict) -> str:
    counts = "\n".join(f"  {t}: {n}" for t, n in meta["row_counts"].items())
    return (
        "Бэкап базы СНО ДГТУ\n"
        f"Создан: {meta['created_at']} (МСК), база: {meta['backend']}\n\n"
        f"ВНИМАНИЕ. {SENSITIVE_NOTE}\n\n"
        "Состав:\n"
        "  backup.json - полный дамп всех таблиц (для восстановления)\n"
        "  csv/*.csv   - те же данные по таблицам (UTF-8, открываются в Excel)\n\n"
        f"Строк по таблицам:\n{counts}\n\n"
        "Не включено: секрет подписи cookie (settings.cookie_secret).\n\n"
        "Восстановление (только вручную, перезаписывает базу целиком):\n"
        "  python restore_backup.py путь/к/бэкапу.zip\n"
        "Скрипт покажет, в какую базу будет запись, сохранит копию текущих данных и спросит подтверждение.\n"
    )


def build_zip(db_path: Any = None) -> bytes:
    """Zip с backup.json + csv/<таблица>.csv + README.txt."""
    data = dump(db_path)
    data["meta"]["sha256_of_tables"] = hashlib.sha256(
        json.dumps(data["tables"], ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
    body = json.dumps(data, ensure_ascii=False, indent=1).encode("utf-8")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("backup.json", body)
        for name, t in data["tables"].items():
            s = io.StringIO()
            w = csv.writer(s, lineterminator="\n")
            w.writerow(t["columns"])
            for r in t["rows"]:
                w.writerow(["" if v is None else v for v in r])
            z.writestr(f"csv/{name}.csv", "\ufeff" + s.getvalue())
        z.writestr("README.txt", _readme(data["meta"]))
    return buf.getvalue()


def default_filename() -> str:
    return "sno_backup_" + audit.now_msk().replace(":", "-").replace("T", "_") + ".zip"


# ── Чтение и восстановление (используется restore_backup.py и тестами) ──────


class BackupError(ValueError):
    """Файл бэкапа повреждён или не подходит (сообщение по-русски)."""


def load_zip(source: Any) -> dict:
    """Прочитать и проверить бэкап (путь или bytes). Возвращает {"meta", "tables"}."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(source) if isinstance(source, (bytes, bytearray)) else source)
        with zf:
            data = json.loads(zf.read("backup.json").decode("utf-8"))
    except (OSError, KeyError, zipfile.BadZipFile, ValueError) as exc:
        raise BackupError(f"Не удалось прочитать бэкап (нужен zip с backup.json): {exc}") from exc
    meta = data.get("meta") or {}
    if meta.get("format") != FORMAT or not isinstance(data.get("tables"), dict):
        raise BackupError("Это не бэкап СНО (неверный формат файла).")
    if int(meta.get("format_version") or 0) > FORMAT_VERSION:
        raise BackupError("Бэкап создан более новой версией сайта - обновите код перед восстановлением.")
    want = meta.get("sha256_of_tables")
    if want:
        got = hashlib.sha256(json.dumps(data["tables"], ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
        if got != want:
            raise BackupError("Контрольная сумма не совпала: файл бэкапа изменён или повреждён.")
    for name, t in data["tables"].items():
        n = len(t.get("columns") or [])
        if not n or any(len(r) != n for r in t.get("rows") or []):
            raise BackupError(f"Таблица {name}: строки не совпадают с колонками - бэкап повреждён.")
    return data


def current_counts(db_path: Any = None) -> dict[str, int]:
    eng = _eng(db_path)
    with eng.connect() as conn:
        return {t: int(conn.execute(text(f"SELECT COUNT(*) FROM {t}")).scalar_one()) for t in table_names(eng)}


def restore(data: dict, db_path: Any = None) -> dict[str, int]:
    """Заменить данные базы содержимым бэкапа ОДНОЙ транзакцией (при ошибке база не меняется).
    Таблицы создаются/обновляются обычным init_db (seed_admin=False). Возвращает {таблица: строк}."""
    db.init_db(db_path=db_path, seed_admin=False)
    eng = _eng(db_path)
    have = set(table_names(eng))
    names = [t for t in table_names(eng) if t in data["tables"]]
    missing = [t for t in data["tables"] if t not in have]
    if missing:
        raise BackupError("В базе нет таблиц из бэкапа: " + ", ".join(missing))
    restored: dict[str, int] = {}
    with eng.begin() as conn:
        for name in reversed(names):  # дети раньше родителей
            if name == "settings":
                conn.execute(text("DELETE FROM settings WHERE key NOT IN ("
                                  + ", ".join(f"'{k}'" for k in EXCLUDED_SETTINGS) + ")"))
            else:
                conn.execute(text(f"DELETE FROM {name}"))
        for name in names:
            t = data["tables"][name]
            real = {c["name"] for c in inspect(eng).get_columns(name)}
            keep = [i for i, c in enumerate(t["columns"]) if c in real]
            cols = [t["columns"][i] for i in keep]
            sql = text(f"INSERT INTO {name} ({', '.join(cols)}) VALUES ({', '.join(':' + c for c in cols)})")
            batch = [{c: r[i] for c, i in zip(cols, keep)} for r in t["rows"]]
            if name == "settings":
                batch = [b for b in batch if b["key"] not in EXCLUDED_SETTINGS]
            for i in range(0, len(batch), 500):
                conn.execute(sql, batch[i:i + 500])
            restored[name] = len(batch)
            if eng.dialect.name == "postgresql" and "id" in real:
                conn.execute(text(
                    f"SELECT setval(pg_get_serial_sequence('{name}', 'id'), "
                    f"COALESCE((SELECT MAX(id) FROM {name}), 0) + 1, false)"))
    db.bump_data_version(db_path)
    return restored
