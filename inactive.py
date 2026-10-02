"""Отчёт для админа «Не заходили и ничего не вносили»: участники без входов и без записей.

Данные: db.list_user_activity() (дата последнего входа и число записей). Выгрузка - Excel (openpyxl, как
в «Что дозаполнить») и CSV (utf-8 с BOM, открывается в Excel без «кракозябр»)."""

from __future__ import annotations

import io
from datetime import datetime
from typing import Optional

import db

# режимы отбора: код -> подпись
MODES = {
    "both": "Не заходили и ничего не внесли",
    "no_login": "Не заходили (записи могли внести за них)",
    "no_records": "Ничего не внесли (заходили или нет)",
}
DEFAULT_MODE = "both"
COLUMNS = ["ФИО", "Логин", "Роль", "Статус", "Создан", "Последний вход (МСК)", "Записей"]


def fmt_dt(value: Optional[str]) -> str:
    """ISO-время -> «дд.мм.гггг чч:мм» (пусто, если нет)."""
    if not value:
        return ""
    try:
        return datetime.fromisoformat(str(value)).strftime("%d.%m.%Y %H:%M")
    except ValueError:
        return str(value)


def list_inactive(mode: str = DEFAULT_MODE, hide_disabled: bool = True, hide_admins: bool = True,
                  db_path: db.DbTarget = None) -> list[dict]:
    """Пользователи по выбранному критерию. hide_disabled - скрыть отключённых, hide_admins - админов."""
    if mode not in MODES:
        raise ValueError(f"Неизвестный режим: {mode}")
    out = []
    for r in db.list_user_activity(db_path):
        if hide_disabled and not r["active"]:
            continue
        if hide_admins and r["role"] == "admin":
            continue
        never = not r["last_login"]
        empty = r["records"] == 0
        if (mode == "both" and never and empty) or (mode == "no_login" and never) or (mode == "no_records" and empty):
            out.append(r)
    return out


def rows_for_export(recs: list[dict]) -> list[dict]:
    return [{
        "ФИО": r["full_name"], "Логин": r["login"],
        "Роль": "админ" if r["role"] == "admin" else "участник",
        "Статус": "активен" if r["active"] else "отключён",
        "Создан": fmt_dt(r["created_at"]),
        "Последний вход (МСК)": fmt_dt(r["last_login"]),
        "Записей": r["records"],
    } for r in recs]


def build_csv(recs: list[dict]) -> bytes:
    import pandas as pd

    return ("\ufeff" + pd.DataFrame(rows_for_export(recs), columns=COLUMNS).to_csv(index=False)).encode("utf-8")


def build_xlsx(recs: list[dict], title: str = "") -> bytes:
    """Excel: лист «Список» (одна строка = участник) и «Сводка»."""
    import pandas as pd

    summary = [{"Показатель": "Критерий", "Значение": title or MODES[DEFAULT_MODE]},
               {"Показатель": "Участников в списке", "Значение": len(recs)},
               {"Показатель": "Выгружено (МСК)", "Значение": datetime.now().strftime("%d.%m.%Y %H:%M")}]
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xw:
        pd.DataFrame(rows_for_export(recs), columns=COLUMNS).to_excel(xw, sheet_name="Список", index=False)
        pd.DataFrame(summary).to_excel(xw, sheet_name="Сводка", index=False)
        for name in ("Список", "Сводка"):
            ws = xw.sheets[name]
            for col in ws.columns:
                width = max((len(str(c.value)) if c.value is not None else 0) for c in col)
                ws.column_dimensions[col[0].column_letter].width = min(max(10, width + 2), 60)
    return buf.getvalue()
