"""«Что дозаполнить»: записи, которые не попадут в годовой отчёт или заполнены не полностью.

Правила НЕ новые - берутся из achievements.py:
  * record_issues(r)  - тексты «заполните ...» (уровень / подпункт / индексация, доли, журнал,
                        тема доклада, учебный год, дубль статьи, главный автор);
  * is_counted(r)     - попадёт ли запись в report_data (нет показателя / нет уровня / дубль);
  * как и в report_data.warnings, публикации «Без индексации» (details.no_index) - валидные и
    сюда не попадают; заочные доклады не в отчёте по замыслу - показываются только по галочке;
  * ach.missing_required(r) - обязательные поля формы, которые у записи пусты (блокируют отчёт);
  * номер достижения (кроме NUMBER_EXEMPT_KINDS) - без него запись не считается (is_counted=False);
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

PLACEHOLDER_PREFIX = ach.PLACEHOLDER_PREFIX

PROBLEM_LABELS = {
    "no_level": "Нет уровня / подпункта",
    "no_indicator": "Нет показателя (не попадёт в отчёт)",
    "no_indexing": "У публикации нет индексации",
    "no_share": "Нет долей участия",
    "no_journal": "Нет журнала",
    "main_author": "Проверить главного автора",
    "no_topic": "Нет темы доклада",
    "no_ayear": "Нет учебного года",
    "no_number": "Нет номера достижения (не попадёт в отчёт)",
    "no_owner": "Не выбран участник",
    "no_date": "Нет даты (не попадёт ни в один отчёт)",
    "dup_number": "Номер повторяется в другой записи",
    "placeholder": "Заглушка «Мероприятие не указано»",
    "duplicate": "Дубль статьи",
    "required": "Пустые обязательные поля",
    "zaochno": "Заочный доклад (в отчёт не входит)",
    "other": "Прочее",
}
# Что считать проблемой по умолчанию: все, кроме заочных докладов (они не в отчёте по замыслу).
DEFAULT_TYPES = [k for k in PROBLEM_LABELS if k != "zaochno"]
# Проблемы, которые НЕ мешают записи попасть в отчёт (achievements._gaps: blocks=False) - только «проверьте».
WARNING_ONLY = frozenset({"main_author", "dup_number"})
# Короткие подписи для сводки в баннере участника («нет уровня / подпункта — 5, нет индексации — 3»).
SHORT_LABELS = {
    "no_level": "нет уровня / подпункта",
    "no_indicator": "нет показателя",
    "no_indexing": "нет индексации",
    "no_share": "нет долей участия",
    "no_journal": "нет журнала",
    "main_author": "проверить главного автора",
    "no_topic": "нет темы доклада",
    "no_ayear": "нет учебного года",
    "no_number": "нет номера достижения",
    "no_owner": "не выбран участник",
    "no_date": "нет даты",
    "dup_number": "номер повторяется",
    "placeholder": "заглушка «Мероприятие не указано»",
    "duplicate": "дубль статьи",
    "required": "пустые обязательные поля",
    "zaochno": "заочный доклад",
    "other": "прочее",
}
# Одна формулировка «почему запись не в отчёте» для подписей в интерфейсе (те же правила, что _gaps).
REASONS_TEXT = ("нет уровня или подпункта; нет номера достижения или он без цифр (номер не нужен только "
                "стипендиям и видам, которые вносит админ); у публикации нет индексации, долей участия или "
                "журнала; у доклада нет темы или вместо мероприятия заглушка «Мероприятие не указано»; у "
                "стипендии нет учебного года; пустые обязательные поля")


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
    if ach.is_dup_number_issue(t):  # «номер «…» уже указан …» - раньше проверки «номер достижения»
        return "dup_number"
    if t.startswith("укажите номер достижения") or t.startswith("номер достижения"):
        return "no_number"
    if t.startswith("уровень / подпункт не относится"):
        return "no_level"
    if t.startswith("укажите участника"):
        return "no_owner"
    if t.startswith("в названии заглушка"):
        return "placeholder"
    if t.startswith("пустые поля"):
        return "required"
    return "other"


def problems(r: dict, viewer: Optional[dict] = None) -> list[tuple[str, str]]:
    """[(код, текст)] для записи; пусто - запись полная и идёт в отчёт. viewer ({id, role}) - кто смотрит:
    для него текст о повторе номера называет другие записи, которые он вправе видеть (ach.issues_for)."""
    out: list[tuple[str, str]] = []
    d = r["details"]
    if d.get("no_index"):  # «Без индексации»: валидная запись вне отчёта, не предупреждение
        return out
    for msg in (ach.issues_for(r, viewer) if viewer is not None else r["issues"]):
        out.append((classify_issue(msg, r["form"]), msg))
    if r["indicator_id"] is None and not any(c == "no_level" for c, _ in out):
        # is_counted(): без показателя запись не считается (грант без подпункта уже объяснён выше)
        out.append(("no_indicator", "нет показателя - запись не попадёт в отчёт"))
    if not r["date_from"]:
        out.append(("no_date", "нет даты - запись не попадёт ни в один годовой отчёт"))
    if not any(c == "required" for c, _ in out):
        # записи вне отчёта по замыслу (заочный доклад, дубль) ach.record_issues на полноту не проверяет
        miss = ach.missing_required(r)
        if miss:
            out.append(("required", "пустые поля: " + ", ".join(miss)))
    if r["form"] == "doklad" and not r["ochno"]:
        out.append(("zaochno", "заочный доклад - в отчёт не входит (так задумано)"))
    return out


def list_todo(year: Optional[int] = None, owner_id: Optional[int] = None,
              types: Optional[list[str]] = None, db_path: Any = None,
              viewer: Optional[dict] = None) -> list[dict]:
    """Записи с проблемами: запись + 'problems' [(код, текст)], 'problem_codes', 'problem_text'.
    types - оставить записи, у которых есть хотя бы одна проблема из списка (None = DEFAULT_TYPES).
    viewer - см. problems() (вкладка админа передаёт админа: в тексте видны все записи с тем же номером)."""
    want = set(types if types is not None else DEFAULT_TYPES)
    out = []
    recs = ach.list_achievements(owner_id=owner_id, year=year or None, db_path=db_path)
    if year:  # записи без даты не попадают ни в один год - показываем в любом периоде
        recs += [r for r in ach.list_achievements(owner_id=owner_id, db_path=db_path) if not r["date_from"]]
    for r in recs:
        ps = [p for p in problems(r, viewer) if p[0] in want]
        if not ps:
            continue
        r = dict(r)
        r["problems"] = ps
        r["problem_codes"] = sorted({c for c, _ in ps})
        r["problem_text"] = "; ".join(t for _, t in ps)
        out.append(r)
    return out


def in_report(r: dict) -> bool:
    """Попадёт ли запись в годовой отчёт своего года: считается (is_counted) и у неё есть дата."""
    return bool(r["counted"] and r["date_from"])


def member_todo(recs: list[dict], viewer: Optional[dict] = None) -> dict:
    """Баннер «Требуют заполнения» в «Мои достижения»: свои записи участника (соавторские - нет, их
    правит внесший) по тем же правилам, что «Что дозаполнить» (problems() без заочных докладов).
      'items'   - {id: {'problems': [(код, текст)], 'in_report': bool}} для каждой записи с проблемами;
      'out'     - id записей, которые НЕ попадут в отчёт, пока их не дозаполнят;
      'check'   - id записей в отчёте, но с замечаниями «проверьте» (WARNING_ONLY);
      'by_type' - {код: сколько записей из 'out' с этой проблемой}, по убыванию.
    viewer - участник, который смотрит (тексты о повторе номера - только с видимыми ему записями)."""
    items: dict[int, dict] = {}
    out: list[int] = []
    check: list[int] = []
    by_type: dict[str, int] = {}
    for r in recs:
        if r.get("role", "owner") != "owner":
            continue
        ps = [p for p in problems(r, viewer) if p[0] != "zaochno"]
        if not ps:
            continue
        ok = in_report(r)
        items[r["id"]] = {"problems": ps, "in_report": ok}
        if ok:
            check.append(r["id"])
            continue
        out.append(r["id"])
        for c in {c for c, _ in ps if c not in WARNING_ONLY}:
            by_type[c] = by_type.get(c, 0) + 1
    by_type = dict(sorted(by_type.items(), key=lambda kv: (-kv[1], list(PROBLEM_LABELS).index(kv[0]))))
    return {"items": items, "out": out, "check": check, "by_type": by_type}


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
