"""«Что дозаполнить»: записи, которые не попадут в годовой отчёт или заполнены не полностью.

Правила НЕ новые - берутся из achievements.py:
  * record_issues(r)  - тексты «заполните ...» (уровень / подпункт / индексация, доли, журнал,
                        тема доклада, учебный год, дубль статьи, главный автор);
  * is_counted(r)     - попадёт ли запись в report_data (нет показателя / нет уровня / дубль);
  * как и в report_data.warnings, публикации «Без индексации» (details.no_index) - валидные и
    сюда не попадают; заочные доклады не в отчёте по замыслу - показываются только по галочке;
  * FORMS[...][required] - обязательные поля формы, которые у записи пусты;
  * заглушка title у докладов «Мероприятие не указано ...» (так импорт из ЛК помечает доклады,
    у которых мероприятие неизвестно).
Каждая найденная проблема получает код (PROBLEM_LABELS); неизвестный текст record_issues попадает
в «Прочее», так что ничего не теряется.
"""

from __future__ import annotations

import io
from typing import Any, Optional

import achievements as ach
import db

PLACEHOLDER_PREFIX = "мероприятие не указано"

PROBLEM_LABELS = {
    "no_level": "Нет уровня / подпункта",
    "no_indicator": "Нет показателя (не попадёт в отчёт)",
    "no_indexing": "У публикации нет индексации",
    "no_share": "Нет долей участия",
    "no_journal": "Нет журнала",
    "main_author": "Проверить главного автора",
    "no_topic": "Нет темы доклада",
    "no_ayear": "Нет учебного года",
    "placeholder": "Заглушка «Мероприятие не указано»",
    "duplicate": "Дубль статьи",
    "required": "Пустые обязательные поля",
    "zaochno": "Заочный доклад (в отчёт не входит)",
    "other": "Прочее",
}
# Что считать проблемой по умолчанию: все, кроме заочных докладов (они не в отчёте по замыслу).
DEFAULT_TYPES = [k for k in PROBLEM_LABELS if k != "zaochno"]

# Поля, которые уже покрыты текстами record_issues (чтобы не дублировать «пустое поле»)
_COVERED_REQUIRED = {"journal", "ayear", "topic"}
# Типы полей, проверяемые другими правилами (доли, даты, список людей)
_SKIP_FTYPES = ("people", "authors", "meeting", "check")


def classify_issue(text_: str, form: str) -> str:
    """Код проблемы по тексту из achievements.record_issues."""
    t = text_.lower()
    if t.startswith("выберите подпункт гранта") or t.startswith("выберите подпункт"):
        return "no_level"
    if t.startswith("выберите индексацию"):
        return "no_indexing"
    if t.startswith("укажите уровень"):
        return "no_level"
    if t.startswith("дубль статьи"):
        return "duplicate"
    if t.startswith("заполните долю"):
        return "no_share"
    if t.startswith("заполните журнал"):
        return "no_journal"
    if t.startswith("проверьте главного автора"):
        return "main_author"
    if t.startswith("заполните тему доклада"):
        return "no_topic"
    if t.startswith("укажите учебный год"):
        return "no_ayear"
    return "other"


def _empty_required(r: dict) -> list[str]:
    """Обязательные поля формы записи, которые пусты (метки без « *» и пояснений)."""
    out = []
    form = r["form"]
    for key, label, ftype, required, _extra in ach.FORMS.get(form, []):
        if not required or ftype in _SKIP_FTYPES or key in _COVERED_REQUIRED:
            continue
        if ftype == "pubdate":
            val = r["date_from"]
        elif ftype == "year":
            val = r["details"].get("year") or r["date_from"]
        elif key in ach._COLUMNS:
            val = r.get(key)
        else:
            val = r["details"].get(key)
        if val is None or (isinstance(val, str) and not val.strip()):
            out.append(label.split(" (")[0])
    return out


def problems(r: dict) -> list[tuple[str, str]]:
    """[(код, текст)] для записи; пусто - запись полная и идёт в отчёт."""
    out: list[tuple[str, str]] = []
    d = r["details"]
    if d.get("no_index"):  # «Без индексации»: валидная запись вне отчёта, не предупреждение
        return out
    for msg in r["issues"]:
        out.append((classify_issue(msg, r["form"]), msg))
    if r["indicator_id"] is None and not any(c == "no_level" for c, _ in out):
        # is_counted(): без показателя запись не считается (грант без подпункта уже объяснён выше)
        out.append(("no_indicator", "нет показателя - запись не попадёт в отчёт"))
    if r["form"] == "doklad" and (r["title"] or "").strip().lower().startswith(PLACEHOLDER_PREFIX):
        out.append(("placeholder", "в названии заглушка «Мероприятие не указано» - впишите мероприятие"))
    miss = _empty_required(r)
    if miss:
        out.append(("required", "пустые поля: " + ", ".join(miss)))
    if r["form"] == "doklad" and not r["ochno"]:
        out.append(("zaochno", "заочный доклад - в отчёт не входит (так задумано)"))
    return out


def list_todo(year: Optional[int] = None, owner_id: Optional[int] = None,
              types: Optional[list[str]] = None, db_path: Any = None) -> list[dict]:
    """Записи с проблемами: запись + 'problems' [(код, текст)], 'problem_codes', 'problem_text'.
    types - оставить записи, у которых есть хотя бы одна проблема из списка (None = DEFAULT_TYPES)."""
    want = set(types if types is not None else DEFAULT_TYPES)
    out = []
    for r in ach.list_achievements(owner_id=owner_id, year=year or None, db_path=db_path):
        ps = [p for p in problems(r) if p[0] in want]
        if not ps:
            continue
        r = dict(r)
        r["problems"] = ps
        r["problem_codes"] = sorted({c for c, _ in ps})
        r["problem_text"] = "; ".join(t for _, t in ps)
        out.append(r)
    return out


def counts_by_type(recs: list[dict]) -> dict[str, int]:
    """Сколько записей с каждым типом проблемы (запись с несколькими проблемами учтена в каждой)."""
    out: dict[str, int] = {}
    for r in recs:
        for c in r["problem_codes"]:
            out[c] = out.get(c, 0) + 1
    return out


def counts_by_owner(recs: list[dict]) -> dict[str, int]:
    out: dict[str, int] = {}
    for r in recs:
        out[r["owner_name"] or "СНО"] = out.get(r["owner_name"] or "СНО", 0) + 1
    return dict(sorted(out.items(), key=lambda kv: (-kv[1], kv[0].casefold())))


def build_xlsx(recs: list[dict], period: str = "") -> bytes:
    """Excel: лист «Что дозаполнить» (одна строка = запись) и «Сводка» (счётчики)."""
    import pandas as pd

    cols = ["ID", "Участник", "Логин", "Вид", "Название", "Дата", "Что дозаполнить", "Типы проблем",
            "В отчёте", "Номер в портфолио"]
    rows = [{
        "ID": r["id"], "Участник": r["owner_name"] or "СНО", "Логин": r["owner_login"] or "",
        "Вид": r["kind_label"], "Название": r["title"], "Дата": ach.fmt_date(r["date_from"]),
        "Что дозаполнить": r["problem_text"],
        "Типы проблем": ", ".join(PROBLEM_LABELS[c] for c in r["problem_codes"]),
        "В отчёте": "да" if r["counted"] else "нет", "Номер в портфолио": r["number"] or "",
    } for r in recs]
    summary = [{"Показатель": "Период", "Значение": period or "за всё время"},
               {"Показатель": "Записей к дозаполнению", "Значение": len(recs)}]
    summary += [{"Показатель": "Тип: " + PROBLEM_LABELS[c], "Значение": n}
                for c, n in counts_by_type(recs).items()]
    summary += [{"Показатель": "Участник: " + n, "Значение": k} for n, k in counts_by_owner(recs).items()]
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xw:
        pd.DataFrame(rows, columns=cols).to_excel(xw, sheet_name="Что дозаполнить", index=False)
        pd.DataFrame(summary).to_excel(xw, sheet_name="Сводка", index=False)
        for name in ("Что дозаполнить", "Сводка"):
            ws = xw.sheets[name]
            for col in ws.columns:
                width = max((len(str(c.value)) if c.value is not None else 0) for c in col)
                ws.column_dimensions[col[0].column_letter].width = min(max(10, width + 2), 70)
    return buf.getvalue()
