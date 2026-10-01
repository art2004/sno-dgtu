#!/usr/bin/env python3
"""Восстановление базы СНО из бэкапа (zip, скачанный кнопкой «Скачать бэкап»).

ВНИМАНИЕ: все данные в целевой базе будут ЗАМЕНЕНЫ содержимым бэкапа. Автоматически из сайта
это не вызывается - только вручную этим скриптом.

Подключение к базе - как у сайта: .streamlit/secrets.toml (DATABASE_URL) -> переменная
окружения DATABASE_URL -> локальная SQLite data/sno.db. Явно: --db-path файл.db или --db-url URL.

Примеры:
    python restore_backup.py sno_backup_2026-10-01_12-00-00.zip --dry-run   # только показать
    python restore_backup.py sno_backup_....zip                            # спросит подтверждение
    python restore_backup.py sno_backup_....zip --db-path data/copy.db

Перед записью скрипт сохраняет копию текущих данных рядом с бэкапом
(before_restore_<время>.zip; отключить: --no-safety-copy) и просит ввести слово ВОССТАНОВИТЬ.
Запись идёт одной транзакцией: при ошибке база остаётся как была.
После восстановления в Streamlit Cloud нажмите «Обновить данные» (или Reboot app).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

try:  # Windows-консоль: не падать на кириллице
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
except Exception:  # noqa: BLE001
    pass

CONFIRM_WORD = "ВОССТАНОВИТЬ"


def _target_label(eng) -> str:  # noqa: ANN001
    url = eng.url
    if eng.dialect.name == "sqlite":
        return f"SQLite: {url.database}"
    return f"Postgres: {url.host}/{url.database} (пользователь {url.username})"


def main(argv=None) -> int:  # noqa: ANN001
    ap = argparse.ArgumentParser(description="Восстановление базы СНО из бэкапа (zip).")
    ap.add_argument("backup", help="файл бэкапа .zip")
    ap.add_argument("--db-path", help="файл SQLite вместо базы по умолчанию")
    ap.add_argument("--db-url", help="URL базы (postgresql://...) вместо базы по умолчанию")
    ap.add_argument("--dry-run", action="store_true", help="только проверить файл и показать, что изменится")
    ap.add_argument("--yes", action="store_true", help="не спрашивать подтверждение (для скриптов; осторожно)")
    ap.add_argument("--no-safety-copy", action="store_true", help="не сохранять копию текущих данных")
    args = ap.parse_args(argv)

    import audit
    import backup
    import db

    target = args.db_url or args.db_path or None
    try:
        data = backup.load_zip(args.backup)
    except backup.BackupError as exc:
        print(f"Ошибка: {exc}")
        return 2
    eng = db.get_engine(target)
    db.init_db(db_path=target, seed_admin=False)
    meta = data["meta"]
    cur = backup.current_counts(target)
    print(f"Бэкап:   {args.backup}\n         создан {meta.get('created_at')} (МСК), база-источник: {meta.get('backend')}")
    print(f"Целевая: {_target_label(eng)}\n")
    print(f"{'таблица':<22}{'сейчас':>10}{'в бэкапе':>12}")
    for t in backup.table_names(target):
        print(f"{t:<22}{cur.get(t, 0):>10}{len(data['tables'].get(t, {}).get('rows', [])):>12}")
    extra = [t for t in data["tables"] if t not in cur]
    if extra:
        print("\nОшибка: в целевой базе нет таблиц из бэкапа: " + ", ".join(extra))
        return 2
    if args.dry_run:
        print("\n--dry-run: ничего не изменено.")
        return 0
    print("\nВСЕ данные в целевой базе будут заменены данными из бэкапа.")
    if not args.yes:
        answer = input(f"Для продолжения введите {CONFIRM_WORD}: ").strip()
        if answer != CONFIRM_WORD:
            print("Отменено, база не изменена.")
            return 1
    if not args.no_safety_copy:
        safety = Path(args.backup).resolve().with_name(
            "before_restore_" + audit.now_msk().replace(":", "-").replace("T", "_") + ".zip")
        safety.write_bytes(backup.build_zip(target))
        print(f"Копия текущих данных: {safety}")
    try:
        restored = backup.restore(data, target)
    except Exception as exc:  # noqa: BLE001
        print(f"Ошибка при восстановлении, база не изменена: {exc.__class__.__name__}: {exc}")
        return 3
    audit.log("backup_restore", actor={"id": None, "login": "restore_backup.py", "full_name": "restore_backup.py"},
              entity="database", summary=f"База восстановлена из бэкапа от {meta.get('created_at')}: "
              + ", ".join(f"{t} {n}" for t, n in restored.items()), db_path=target)
    print("Готово: " + ", ".join(f"{t}={n}" for t, n in restored.items()))
    print("В Streamlit Cloud нажмите «Обновить данные» (или Reboot app).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
