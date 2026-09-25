"""Достижения и годовой отчёт СНО: каталог 18 показателей формы, записи, миграция, подсчёт.

Модель:
  indicators / indicator_rows — показатели формы отчёта и их подпункты (уровни или
      подвиды), формулировки дословно из формы; сидируются идемпотентно, админ может
      переименовать, переставить, добавить, скрыть (удалить — только без записей).
  kinds / kind_subpoints — «виды» для участника (Доклад, Публикация, Грант …) и их
      привязка к показателям. У вида с indicator_id подпункты = подпункты показателя;
      у «Гранта» свои подпункты (заявка → п.7 + уровень; работы → строки п.9).
  achievements — одна запись = одно достижение одного человека (владелец, номер).
      1 запись = 1 единица в отчёте; публикация с соавторами — одна запись главного
      автора, соавторы в achievement_people (с долями).
  achievement_people — соавторы/участники записи (участник СНО или внешний, доля %).
Старые таблицы events/participations не удаляются: участия переносятся в achievements
(achievements.legacy_pid / achievement_people.legacy_pid → повторный запуск ничего не
дублирует), events остаётся «мероприятием» для группировки докладов и связи с заседаниями.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime
from typing import Any, Optional

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine

import db
from db import DbTarget, _insert_returning_id, _now, _one, _rows, get_engine, normalize_title

# ── Каталог (дословно по форме отчёта) ──────────────────────────────────────

_LV = ("intl", "ru", "reg", "univ")


def _lv(labels: list[str], codes: tuple = _LV) -> list[tuple[str, str]]:
    return list(zip(codes, labels))


_L1 = ["-международный уровень", "-всероссийский уровень", "-региональный уровень",
       "-внутривузовский уровень (без статуса)"]
_L2 = ["- международный уровень", "- всероссийский уровень", "- региональный уровень",
       "- внутривузовский уровень (без статуса)"]

# (code, label, dimension, rows[(code, label)])
INDICATORS: list[tuple[str, str, str, list[tuple[str, str]]]] = [
    ("I01", "1.Выступление с научным докладом (только очное участие в конференции, семинаре, "
            "форуме, симпозиуме)", "level", _lv(_L1)),
    ("I02", "2. Научные публикации", "subtype", [
        ("scopus", "-статья, индексируемая в базе «Scopus» и/или «Web of Science Core Collection»"),
        ("scopus_ar", "-статья, индексируемая в базе «Scopus» и/или «Web of Science Core Collection» "
                      "типа Article и Review"),
        ("vak", "-статья в журнале из перечня ВАК / RSCI"),
        ("rinc", "-статья в журнале из перечня РИНЦ"),
        ("white", "- статья в журнале из перечня Белый список"),
    ]),
    ("I03", "3.Участие в конкурсе научных работ (без финансовой составляющей, конкурсы научных "
            "статей не учитываются), кейс-чемпионате (научные, научно-практические), медали, "
            "грамоты, дипломы, премии и пр. полученное в рамках конкурсов", "level", _lv(_L1)),
    ("I04", "4.Участие в образовательных мероприятиях и проектах научной направленности "
            "(научные, научно-практические)", "level", _lv(_L1)),
    ("I05", "5.Участие в выставке", "level", _lv(_L1)),
    ("I06", "6.Охранный документ", "subtype", [
        ("patent", "-патент"),
        ("patent_app", "-заявка на патент"),
        ("software", "-свидетельство о государственной регистрации программы ЭВМ или базы данных"),
        ("license", "-проданная лицензия на право использования объекта интеллектуальной собственности"),
    ]),
    ("I07", "7.Участие в оформлении заявки на получение гранта на реализацию "
            "научно-исследовательского / научно-популяризационного проекта, аффилированного с ДГТУ",
     "level", _lv(["- международный уровень", "- всероссийский уровень", "- региональный уровень",
                   "- городской уровень"], ("intl", "ru", "reg", "city"))),
    ("I08", "8.Назначение именной стипендии", "subtype", [
        ("president", "- стипендия Президента Российской Федерации"),
        ("government", "- стипендии Правительства Российской Федерации"),
        ("regional", "- именная стипендия всероссийского/ окружного/ областного уровня"),
        ("local", "- именная стипендия органов местного самоуправления"),
        ("other", "- иные именные стипендии"),
    ]),
    ("I09", "9.Участие в научной работе", "subtype", [
        ("pp", "-научно-исследовательские, опытно-конструкторские, технологические работы, "
               "выполняемые в рамках ПП №218, ПП №220, а также прочих научных программах и конкурсах"),
        ("rnf", "- проекты, выполняемые при поддержке РНФ"),
        ("funds", "- проекты, выполняемые при поддержке прочих научных фондов"),
        ("hoz", "- хоздоговорная научно-исследовательская работа"),
    ]),
    ("I10", "10.Участие в программе научного обмена", "level",
     _lv(["- международный уровень", "- всероссийский уровень"], ("intl", "ru"))),
    ("I11", "11.Организация научного мероприятия (научная, научно-практическая конференция, "
            "конкурс на лучшую НИР обучающихся и др.)", "level", _lv(_L2)),
    ("I12", "12.Организация научно-популярного мероприятия (круглый стол, творческий конкурс "
            "научно-технических разработок, выставка, конгресс, саммит, симпозиум, научный "
            "семинар, коллоквиум и др.)", "level", _lv(_L2[:3] + ["-внутривузовский уровень (без статуса)"])),
    ("I13", "13.Результаты участия в конкурсах Фонда содействия инновациям", "subtype", [
        ("umnik_p", "- Программа «УМНИК» - участие"),
        ("umnik_w", "- Программа «УМНИК» - лауреат"),
        ("start_p", "- Программа «СТАРТ» - участие"),
        ("start_w", "- Программа «СТАРТ» - лауреат"),
        ("comm_p", "- Программа «Коммерциализация» - участие"),
        ("comm_w", "- Программа «Коммерциализация» - лауреат"),
        ("stud_p", "- Программа «Студенческий стартап» - участие"),
        ("stud_w", "- Программа «Студенческий стартап» - лауреат"),
    ]),
    ("I14", "14.Научные премии", "level",
     _lv(["- международный уровень", "- всероссийский уровень",
          "- межрегиональный/региональный уровень", "- городской уровень"],
         ("intl", "ru", "reg", "city"))),
    ("I15", "15.Научное волонтерство", "level",
     _lv(_L2[:3] + ["-внутривузовский уровень (без статуса)"])),
    ("I16", "16.Участие в региональном/федеральном/отраслевом конкурсе оценки деятельности "
            "студенческих научных сообществ", "subtype", [("part", "- участие")]),
    ("I17", "17.Соглашение и совместные проекты с партнерами академическими и/или бизнес- "
            "партнерами ", "none", []),
    ("I18", "18. Работы, финансируемые из бюджетных и внебюджетных источников ", "none", []),
]

# (code, label, form, indicator_code, admin_only)
KINDS: list[tuple[str, str, str, Optional[str], int]] = [
    ("doklad", "Доклад", "doklad", "I01", 0),
    ("publication", "Публикация", "publication", "I02", 0),
    ("contest", "Конкурс / кейс-чемпионат", "contest", "I03", 0),
    ("edu", "Образовательное мероприятие", "event", "I04", 0),
    ("expo", "Выставка", "expo", "I05", 0),
    ("ip", "Охранный документ", "ip", "I06", 0),
    ("grant", "Грант", "grant", None, 0),
    ("stipend", "Стипендия", "stipend", "I08", 0),
    ("exchange", "Научный обмен", "exchange", "I10", 0),
    ("fsi", "Фонд содействия инновациям", "fsi", "I13", 0),
    ("prize", "Научная премия", "prize", "I14", 0),
    ("volunteer", "Научное волонтёрство", "event", "I15", 0),
    ("org_sci", "Организация научного мероприятия", "org", "I11", 1),
    ("org_pop", "Организация научно-популярного мероприятия", "org", "I12", 1),
    ("sno_contest", "Конкурс оценки СНО", "sno_contest", "I16", 1),
    ("agreement", "Соглашение с партнёром", "agreement", "I17", 1),
    ("funded", "Финансируемая работа", "funded", "I18", 1),
]

# (code, kind_code, label, indicator_code, row_code | None, form)
SUBPOINTS: list[tuple[str, str, str, str, Optional[str], str]] = [
    ("grant.app", "grant", "Заявка на грант", "I07", None, "grant_app"),
    ("grant.rnf", "grant", "Работа по гранту РНФ", "I09", "I09.rnf", "work"),
    ("grant.funds", "grant", "Прочие фонды", "I09", "I09.funds", "work"),
    ("grant.pp", "grant", "ПП №218/220 и программы", "I09", "I09.pp", "work"),
    ("grant.hoz", "grant", "Хоздоговор", "I09", "I09.hoz", "work"),
]

# Поля форм: (key, label, type, required, extra). Типы: text, textarea, date, date_opt,
# select, check, people, authors, year, ayear, meeting. Ключи title/date_from/date_to/topic/
# number/link/ochno — колонки achievements; остальные — details (JSON).
_TAIL = [("number", "Номер достижения (Р-Н-…, с сайта вуза)", "text", False, {}),
         ("link", "Ссылка на подтверждение", "text", False, {})]
CONTEST_RESULTS = ("участие", "диплом", "призёр", "лауреат")
NO_INDEX = -2  # публикация «Без индексации»: валидная запись, в годовой отчёт не входит
NO_INDEX_LABEL = "без индексации (в годовой отчёт не входит)"
GRANT_STATUSES = ("подана", "поддержана")
FUND_SOURCES = ("бюджет", "внебюджет")

FORMS: dict[str, list[tuple]] = {
    "doklad": [
        ("title", "Мероприятие (конференция, форум, семинар…)", "text", True, {}),
        ("date_from", "Дата мероприятия", "date", True, {}),
        ("topic", "Тема доклада", "text", True, {}),
        ("ochno", "Очное участие", "check", False,
         {"default": True, "help": "Заочные доклады сохраняются, но в отчёт не попадают"}),
        *_TAIL,
    ],
    "publication": [
        ("title", "Название статьи", "text", True, {}),
        ("authors", "Авторы и доли участия", "authors", True, {}),
        ("authors_text", "Авторы, как в статье (необязательно)", "text", False,
         {"help": "Например: Мартынюк И.О., Старостин Д.В. Если пусто — список соберётся из строк авторов."}),
        ("journal", "Журнал / сборник", "text", True, {}),
        ("year", "Год публикации", "year", True, {}),
        ("link", "Ссылка", "text", False, {}),
        ("doi", "DOI", "text", False, {}),
        ("number", "Номер достижения (Р-Н-…, с сайта вуза)", "text", False, {}),
        ("bib", "Библиографическая ссылка (необязательно)", "textarea", False,
         {"help": "Если заполнено — в отчёт попадёт как есть (доли участия допишутся, если их нет)."}),
    ],
    "contest": [
        ("title", "Конкурс / кейс-чемпионат", "text", True, {}),
        ("date_from", "Дата начала", "date", True, {}),
        ("date_to", "Дата окончания (если несколько дней)", "date_opt", False, {}),
        ("result", "Результат (необязательно)", "text", False,
         {"help": "Например: диплом II степени, 1 место, лауреат. Печатается в конце строки отчёта."}),
        ("people", "Участники", "people", False, {}),
        *_TAIL,
    ],
    "event": [
        ("title", "Название мероприятия / проекта", "text", True, {}),
        ("date_from", "Дата", "date", True, {}),
        ("date_to", "Дата окончания (если несколько дней)", "date_opt", False, {}),
        ("people", "Участники", "people", False, {}),
        *_TAIL,
    ],
    "prize": [
        ("title", "Название премии", "text", True, {}),
        ("date_from", "Дата", "date", True, {}),
        ("people", "Участники", "people", False, {}),
        *_TAIL,
    ],
    "expo": [
        ("title", "Выставка", "text", True, {}),
        ("date_from", "Дата", "date", True, {}),
        ("date_to", "Дата окончания (если несколько дней)", "date_opt", False, {}),
        ("exhibit", "Экспонат", "text", False, {}),
        ("people", "Участники", "people", False, {}),
        *_TAIL,
    ],
    "ip": [
        ("title", "Название объекта (изобретение, программа, база данных…)", "text", True, {}),
        ("doc_number", "Номер документа / заявки", "text", True, {}),
        ("date_from", "Дата документа", "date", True, {}),
        ("people", "Соавторы", "people", False, {}),
        *_TAIL,
    ],
    "grant_app": [
        ("title", "Фонд / конкурс", "text", True, {}),
        ("project", "Название проекта", "text", True, {}),
        ("date_from", "Дата подачи", "date", True, {}),
        ("status", "Статус заявки", "select", True, {"options": GRANT_STATUSES}),
        ("people", "Участники", "people", False, {}),
        *_TAIL,
    ],
    "work": [
        ("title", "Номер договора / соглашения / гранта", "text", True,
         {"help": "Например: РНФ 25-19-00523 или 06-25-УНИ"}),
        ("date_from", "Дата", "date", True, {}),
        ("topic", "Тема", "text", True, {}),
        ("people", "Участники", "people", False, {}),
        *_TAIL,
    ],
    "grant": [  # «Грант» без выбранного подпункта (только перенесённые записи)
        ("title", "Название", "text", True, {}),
        ("date_from", "Дата", "date", True, {}),
        *_TAIL,
    ],
    "stipend": [
        ("title", "Название стипендии", "text", True, {}),
        ("ayear", "Учебный год", "ayear", True, {}),
        *_TAIL,
    ],
    "exchange": [
        ("title", "Программа обмена", "text", True, {}),
        ("org", "Организация / страна", "text", False, {}),
        ("date_from", "Начало", "date", True, {}),
        ("date_to", "Окончание", "date_opt", False, {}),
        *_TAIL,
    ],
    "fsi": [
        ("title", "Название проекта", "text", True, {}),
        ("date_from", "Дата (результата / подачи)", "date", True, {}),
        ("people", "Участники", "people", False, {}),
        *_TAIL,
    ],
    "org": [
        ("meeting", "Из «Заседаний и мероприятий» (необязательно)", "meeting", False, {}),
        ("title", "Название мероприятия", "text", True, {}),
        ("date_from", "Дата проведения", "date", True, {}),
        ("order_no", "Номер приказа", "text", False, {}),
        ("order_date", "Дата приказа", "date_opt", False, {}),
        ("order_subject", "Содержание приказа («Об организации …»)", "text", False, {}),
        ("people", "Участники-организаторы", "people", False, {}),
        ("link", "Ссылка на подтверждение", "text", False, {}),
    ],
    "sno_contest": [
        ("title", "Конкурс", "text", True, {}),
        ("date_from", "Дата", "date", True, {}),
        ("people", "Участники", "people", False, {}),
        *_TAIL,
    ],
    "agreement": [
        ("title", "Партнёр", "text", True, {}),
        ("agreement", "Номер и дата соглашения", "text", True, {}),
        ("subject", "Предмет соглашения / совместный проект", "text", False, {}),
        ("date_from", "Дата соглашения", "date", True, {}),
        ("link", "Ссылка на подтверждение", "text", False, {}),
    ],
    "funded": [
        ("title", "Название работы", "text", True, {}),
        ("source", "Источник", "select", True, {"options": FUND_SOURCES}),
        ("contract", "Договор / программа финансирования", "text", False, {}),
        ("date_from", "Начало", "date", True, {}),
        ("date_to", "Окончание", "date_opt", False, {}),
        *_TAIL,
    ],
}
_COLUMNS = ("title", "date_from", "date_to", "topic", "number", "link", "ochno")
# виды, записи которых группируются по мероприятию (events): доклады, конкурсы и т.п.
EVENT_KINDS = {"doklad": "конференция", "contest": "конкурс", "edu": "образовательное",
               "expo": "выставка", "volunteer": "волонтёрство"}
NUMBER_MAX_LEN = 64


class DuplicateAchievement(ValueError):
    """Такая публикация уже внесена (по DOI или названию + году)."""


# ── Schema ──────────────────────────────────────────────────────────────────

_SCHEMA = [
    """CREATE TABLE IF NOT EXISTS indicators (
        id {pk}, code TEXT NOT NULL UNIQUE, label TEXT NOT NULL,
        sort_order INTEGER NOT NULL DEFAULT 0, dimension TEXT NOT NULL DEFAULT 'none',
        hidden INTEGER NOT NULL DEFAULT 0)""",
    """CREATE TABLE IF NOT EXISTS indicator_rows (
        id {pk}, indicator_id INTEGER NOT NULL REFERENCES indicators(id),
        code TEXT NOT NULL UNIQUE, label TEXT NOT NULL,
        sort_order INTEGER NOT NULL DEFAULT 0, hidden INTEGER NOT NULL DEFAULT 0)""",
    """CREATE TABLE IF NOT EXISTS kinds (
        id {pk}, code TEXT NOT NULL UNIQUE, label TEXT NOT NULL, form TEXT NOT NULL,
        indicator_id INTEGER REFERENCES indicators(id), sort_order INTEGER NOT NULL DEFAULT 0,
        hidden INTEGER NOT NULL DEFAULT 0, admin_only INTEGER NOT NULL DEFAULT 0)""",
    """CREATE TABLE IF NOT EXISTS kind_subpoints (
        id {pk}, kind_id INTEGER NOT NULL REFERENCES kinds(id), code TEXT NOT NULL UNIQUE,
        label TEXT NOT NULL, indicator_id INTEGER NOT NULL REFERENCES indicators(id),
        row_id INTEGER REFERENCES indicator_rows(id), form TEXT,
        sort_order INTEGER NOT NULL DEFAULT 0, hidden INTEGER NOT NULL DEFAULT 0)""",
    """CREATE TABLE IF NOT EXISTS achievements (
        id {pk},
        kind_id INTEGER NOT NULL REFERENCES kinds(id),
        subpoint_id INTEGER REFERENCES kind_subpoints(id),
        indicator_id INTEGER REFERENCES indicators(id),
        row_id INTEGER REFERENCES indicator_rows(id),
        owner_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
        title TEXT NOT NULL DEFAULT '',
        date_from TEXT, date_to TEXT, number TEXT, link TEXT, topic TEXT,
        ochno INTEGER NOT NULL DEFAULT 1,
        owner_share REAL,
        externals TEXT,
        details TEXT NOT NULL DEFAULT '{{}}',
        event_id INTEGER REFERENCES events(id) ON DELETE SET NULL,
        meeting_id INTEGER REFERENCES meetings(id) ON DELETE SET NULL,
        legacy_pid INTEGER UNIQUE,
        created_at TEXT NOT NULL, created_by INTEGER, updated_at TEXT)""",
    "CREATE INDEX IF NOT EXISTS idx_ach_owner ON achievements(owner_id)",
    "CREATE INDEX IF NOT EXISTS idx_ach_indicator ON achievements(indicator_id)",
    "CREATE INDEX IF NOT EXISTS idx_ach_event ON achievements(event_id)",
    """CREATE TABLE IF NOT EXISTS achievement_people (
        id {pk},
        achievement_id INTEGER NOT NULL REFERENCES achievements(id) ON DELETE CASCADE,
        sort_order INTEGER NOT NULL DEFAULT 0,
        user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
        name TEXT NOT NULL, share REAL, legacy_pid INTEGER UNIQUE)""",
    "CREATE INDEX IF NOT EXISTS idx_ach_people ON achievement_people(achievement_id)",
]


def _eng(db_path: DbTarget) -> Engine:
    return db_path if isinstance(db_path, Engine) else get_engine(db_path)


def ensure_schema(db_path: DbTarget = None) -> dict:
    """Create tables, seed the catalog, migrate legacy participations. Idempotent.
    Called from db._create_schema() on every start."""
    eng = _eng(db_path)
    pk = ("INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY"
          if eng.dialect.name == "postgresql" else "INTEGER PRIMARY KEY AUTOINCREMENT")
    with eng.begin() as conn:
        for stmt in _SCHEMA:
            conn.execute(text(stmt.format(pk=pk)))
    with eng.begin() as conn:
        seed_catalog(conn)
    with eng.begin() as conn:
        return migrate_legacy(conn)


def seed_catalog(conn) -> int:  # noqa: ANN001
    """Insert missing indicators/rows/kinds/subpoints by code; never touches existing ones
    (admin renames, order and hidden flags survive restarts). Returns inserted count."""
    n = 0
    ind_ids: dict[str, int] = {r[0]: int(r[1]) for r in conn.execute(text("SELECT code, id FROM indicators"))}
    row_ids: dict[str, int] = {r[0]: int(r[1]) for r in conn.execute(text("SELECT code, id FROM indicator_rows"))}
    for pos, (code, label, dim, rows) in enumerate(INDICATORS, start=1):
        if code not in ind_ids:
            ind_ids[code] = _insert_returning_id(
                conn, "INSERT INTO indicators (code, label, sort_order, dimension) VALUES (:c, :l, :p, :d)",
                {"c": code, "l": label, "p": pos * 10, "d": dim})
            n += 1
        for rpos, (rc, rl) in enumerate(rows, start=1):
            full = f"{code}.{rc}"
            if full not in row_ids:
                row_ids[full] = _insert_returning_id(
                    conn, "INSERT INTO indicator_rows (indicator_id, code, label, sort_order) "
                          "VALUES (:i, :c, :l, :p)", {"i": ind_ids[code], "c": full, "l": rl, "p": rpos * 10})
                n += 1
    kind_ids: dict[str, int] = {r[0]: int(r[1]) for r in conn.execute(text("SELECT code, id FROM kinds"))}
    for pos, (code, label, form, ind, admin_only) in enumerate(KINDS, start=1):
        if code not in kind_ids:
            kind_ids[code] = _insert_returning_id(
                conn, "INSERT INTO kinds (code, label, form, indicator_id, sort_order, admin_only) "
                      "VALUES (:c, :l, :f, :i, :p, :a)",
                {"c": code, "l": label, "f": form, "i": ind_ids.get(ind) if ind else None,
                 "p": pos * 10, "a": admin_only})
            n += 1
    have = set(conn.execute(text("SELECT code FROM kind_subpoints")).scalars())
    for pos, (code, kind, label, ind, row, form) in enumerate(SUBPOINTS, start=1):
        if code not in have:
            conn.execute(text(
                "INSERT INTO kind_subpoints (kind_id, code, label, indicator_id, row_id, form, sort_order) "
                "VALUES (:k, :c, :l, :i, :r, :f, :p)"),
                {"k": kind_ids[kind], "c": code, "l": label, "i": ind_ids[ind],
                 "r": row_ids.get(row) if row else None, "f": form, "p": pos * 10})
            n += 1
    return n


# ── Normalization helpers ───────────────────────────────────────────────────

_LAT2CYR = str.maketrans("PHXOCEAKMTBY", "РНХОСЕАКМТВУ")


def normalize_number(value: Optional[str]) -> Optional[str]:
    """«№ P-H-1700-25» → «Р-Н-1700-25»: strip №/Nº, Latin look-alikes → Cyrillic, dashes."""
    s = (value or "").strip()
    s = re.sub(r"^(№|N[º°o]\.?)\s*", "", s, flags=re.I).strip()
    if not s:
        return None
    s = re.sub(r"[‐‑‒–—−]", "-", s)
    s = re.sub(r"\s*-\s*", "-", s)
    s = s.upper().translate(_LAT2CYR)
    if len(s) > NUMBER_MAX_LEN:
        raise ValueError(f"Номер достижения не длиннее {NUMBER_MAX_LEN} символов")
    return s


def normalize_doi(value: Optional[str]) -> str:
    s = (value or "").strip().lower()
    s = re.sub(r"^(https?://)?(dx\.)?doi\.org/", "", s)
    s = re.sub(r"^doi:\s*", "", s)
    return s


def academic_year_range(ay: str) -> tuple[str, str]:
    m = re.match(r"^\s*(\d{4})\s*[-/–]\s*(\d{4})\s*$", ay or "")
    if not m or int(m.group(2)) != int(m.group(1)) + 1:
        raise ValueError("Учебный год в формате 2025-2026")
    return f"{m.group(1)}-09-01", f"{m.group(2)}-08-31"


def academic_years(around: Optional[int] = None, back: int = 4, ahead: int = 1) -> list[str]:
    y = around or date.today().year
    return [f"{a}-{a + 1}" for a in range(y + ahead, y - back - 1, -1)]


def _iso(value: Any) -> Optional[str]:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return date.fromisoformat(str(value)[:10]).isoformat()


def fmt_date(value: Any) -> str:
    iso = _iso(value)
    return date.fromisoformat(iso).strftime("%d.%m.%Y") if iso else ""


def strip_dash(label: str) -> str:
    return re.sub(r"^\s*-\s*", "", label or "").strip()


def _share_str(v: Optional[float]) -> str:
    if v is None:
        return "?"
    return f"{v:g}%"


# ── Catalog reads & admin edits ─────────────────────────────────────────────


@db.cached
def catalog(db_path: DbTarget = None, include_hidden: bool = True) -> list[dict]:
    """Indicators ordered, each with 'rows' (ordered) and 'records' count."""
    with _eng(db_path).connect() as conn:
        inds = _rows(conn.execute(text("SELECT * FROM indicators ORDER BY sort_order, id")))
        rows = _rows(conn.execute(text("SELECT * FROM indicator_rows ORDER BY sort_order, id")))
        cnt_i = dict(conn.execute(text(
            "SELECT indicator_id, COUNT(*) FROM achievements WHERE indicator_id IS NOT NULL "
            "GROUP BY indicator_id")).all())
        cnt_r = dict(conn.execute(text(
            "SELECT row_id, COUNT(*) FROM achievements WHERE row_id IS NOT NULL GROUP BY row_id")).all())
    by_ind: dict[int, list[dict]] = {}
    for r in rows:
        r["records"] = int(cnt_r.get(r["id"], 0))
        if include_hidden or not r["hidden"]:
            by_ind.setdefault(r["indicator_id"], []).append(r)
    out = []
    for i in inds:
        if not include_hidden and i["hidden"]:
            continue
        i["rows"] = by_ind.get(i["id"], [])
        i["records"] = int(cnt_i.get(i["id"], 0))
        out.append(i)
    return out


@db.cached
def indicator_rows(indicator_id: int, db_path: DbTarget = None, include_hidden: bool = False) -> list[dict]:
    with _eng(db_path).connect() as conn:
        return _rows(conn.execute(text(
            "SELECT * FROM indicator_rows WHERE indicator_id = :i"
            + ("" if include_hidden else " AND hidden = 0") + " ORDER BY sort_order, id"),
            {"i": indicator_id}))


@db.cached
def _all_kinds(db_path: DbTarget = None) -> list[dict]:
    with _eng(db_path).connect() as conn:
        kinds = _rows(conn.execute(text("SELECT * FROM kinds ORDER BY sort_order, id")))
        subs = _rows(conn.execute(text("SELECT * FROM kind_subpoints ORDER BY sort_order, id")))
    for k in kinds:
        k["subpoints"] = [sp for sp in subs if sp["kind_id"] == k["id"]]
    return kinds


def list_kinds(db_path: DbTarget = None, admin: bool = False, include_hidden: bool = False) -> list[dict]:
    kinds = [k for k in _all_kinds(db_path)
             if (admin or not k["admin_only"]) and (include_hidden or not k["hidden"])]
    if not include_hidden:
        for k in kinds:
            k["subpoints"] = [sp for sp in k["subpoints"] if not sp["hidden"]]
    return kinds


@db.cached
def get_kind(kind_id: int, db_path: DbTarget = None) -> Optional[dict]:
    kinds = [k for k in list_kinds(db_path, admin=True, include_hidden=True) if k["id"] == kind_id]
    return kinds[0] if kinds else None


@db.cached
def kind_by_code(code: str, db_path: DbTarget = None) -> dict:
    for k in list_kinds(db_path, admin=True, include_hidden=True):
        if k["code"] == code:
            return k
    raise KeyError(code)


@db.cached
def row_by_code(code: str, db_path: DbTarget = None) -> dict:
    with _eng(db_path).connect() as conn:
        r = _one(conn.execute(text("SELECT * FROM indicator_rows WHERE code = :c"), {"c": code}))
    if r is None:
        raise KeyError(code)
    return r


def update_indicator(indicator_id: int, label: str, hidden: bool, sort_order: Optional[int] = None,
                     db_path: DbTarget = None) -> None:
    label = (label or "").strip()
    if not label:
        raise ValueError("Формулировка показателя не может быть пустой")
    sql = "UPDATE indicators SET label = :l, hidden = :h"
    params: dict[str, Any] = {"l": label, "h": int(bool(hidden)), "id": indicator_id}
    if sort_order is not None:
        sql += ", sort_order = :s"
        params["s"] = int(sort_order)
    with _eng(db_path).begin() as conn:
        conn.execute(text(sql + " WHERE id = :id"), params)


def update_row(row_id: int, label: str, hidden: bool, sort_order: Optional[int] = None,
               db_path: DbTarget = None) -> None:
    label = (label or "").strip()
    if not label:
        raise ValueError("Формулировка подпункта не может быть пустой")
    sql = "UPDATE indicator_rows SET label = :l, hidden = :h"
    params: dict[str, Any] = {"l": label, "h": int(bool(hidden)), "id": row_id}
    if sort_order is not None:
        sql += ", sort_order = :s"
        params["s"] = int(sort_order)
    with _eng(db_path).begin() as conn:
        conn.execute(text(sql + " WHERE id = :id"), params)


def _next_code(conn, table: str, prefix: str) -> str:  # noqa: ANN001
    codes = set(conn.execute(text(f"SELECT code FROM {table}")).scalars())
    i = 1
    while f"{prefix}{i}" in codes:
        i += 1
    return f"{prefix}{i}"


def add_indicator(label: str, dimension: str = "level", db_path: DbTarget = None) -> int:
    """New indicator + a matching member kind (generic event form) so records can be added."""
    label = (label or "").strip()
    if not label:
        raise ValueError("Укажите формулировку показателя")
    if dimension not in ("level", "subtype", "none"):
        raise ValueError("Недопустимое измерение")
    with _eng(db_path).begin() as conn:
        pos = int(conn.execute(text("SELECT COALESCE(MAX(sort_order), 0) FROM indicators")).scalar_one()) + 10
        iid = _insert_returning_id(
            conn, "INSERT INTO indicators (code, label, sort_order, dimension) VALUES (:c, :l, :p, :d)",
            {"c": _next_code(conn, "indicators", "U"), "l": label, "p": pos, "d": dimension})
        _insert_returning_id(
            conn, "INSERT INTO kinds (code, label, form, indicator_id, sort_order, admin_only) "
                  "VALUES (:c, :l, 'event', :i, :p, 0)",
            {"c": _next_code(conn, "kinds", "custom"), "l": re.sub(r"^\d+\.\s*", "", label)[:60],
             "i": iid, "p": 1000 + pos})
        return iid


def add_row(indicator_id: int, label: str, db_path: DbTarget = None) -> int:
    label = (label or "").strip()
    if not label:
        raise ValueError("Укажите формулировку подпункта")
    with _eng(db_path).begin() as conn:
        code = conn.execute(text("SELECT code FROM indicators WHERE id = :i"), {"i": indicator_id}).scalar()
        if code is None:
            raise ValueError("Показатель не найден")
        pos = int(conn.execute(text(
            "SELECT COALESCE(MAX(sort_order), 0) FROM indicator_rows WHERE indicator_id = :i"),
            {"i": indicator_id}).scalar_one()) + 10
        rid = _insert_returning_id(
            conn, "INSERT INTO indicator_rows (indicator_id, code, label, sort_order) VALUES (:i, :c, :l, :p)",
            {"i": indicator_id, "c": _next_code(conn, "indicator_rows", f"{code}.u"), "l": label, "p": pos})
        conn.execute(text("UPDATE indicators SET dimension = 'subtype' WHERE id = :i AND dimension = 'none'"),
                     {"i": indicator_id})
        return rid


def delete_row(row_id: int, db_path: DbTarget = None) -> None:
    with _eng(db_path).begin() as conn:
        n = int(conn.execute(text("SELECT COUNT(*) FROM achievements WHERE row_id = :r"), {"r": row_id}).scalar_one())
        if n:
            raise ValueError(f"По подпункту есть записи ({n}) — его можно только скрыть.")
        if conn.execute(text("SELECT 1 FROM kind_subpoints WHERE row_id = :r"), {"r": row_id}).first():
            raise ValueError("Подпункт используется в «Гранте» — его можно только скрыть.")
        conn.execute(text("DELETE FROM indicator_rows WHERE id = :r"), {"r": row_id})


def delete_indicator(indicator_id: int, db_path: DbTarget = None) -> None:
    with _eng(db_path).begin() as conn:
        n = int(conn.execute(text(
            "SELECT COUNT(*) FROM achievements WHERE indicator_id = :i OR row_id IN "
            "(SELECT id FROM indicator_rows WHERE indicator_id = :i)"), {"i": indicator_id}).scalar_one())
        if n:
            raise ValueError(f"По показателю есть записи ({n}) — его можно только скрыть.")
        if conn.execute(text("SELECT 1 FROM kind_subpoints WHERE indicator_id = :i"), {"i": indicator_id}).first():
            raise ValueError("Показатель используется в подпунктах «Гранта» — его можно только скрыть.")
        conn.execute(text("DELETE FROM indicator_rows WHERE indicator_id = :i"), {"i": indicator_id})
        conn.execute(text("DELETE FROM kinds WHERE indicator_id = :i AND id NOT IN "
                          "(SELECT kind_id FROM achievements)"), {"i": indicator_id})
        conn.execute(text("UPDATE kinds SET indicator_id = NULL, hidden = 1 WHERE indicator_id = :i"),
                     {"i": indicator_id})
        conn.execute(text("DELETE FROM indicators WHERE id = :i"), {"i": indicator_id})


def move_indicator(indicator_id: int, direction: int, db_path: DbTarget = None) -> None:
    _move("indicators", item_id=indicator_id, direction=direction, parent=None, db_path=db_path)


def move_row(row_id: int, direction: int, db_path: DbTarget = None) -> None:
    with _eng(db_path).connect() as conn:
        ind = conn.execute(text("SELECT indicator_id FROM indicator_rows WHERE id = :r"), {"r": row_id}).scalar()
    _move("indicator_rows", item_id=row_id, direction=direction, parent=ind, db_path=db_path)


def _move(table: str, item_id: int, direction: int, parent: Optional[int], db_path: DbTarget) -> None:
    where, params = ("", {}) if parent is None else (" WHERE indicator_id = :par", {"par": int(parent)})
    with _eng(db_path).begin() as conn:
        ids = list(conn.execute(text(f"SELECT id FROM {table}{where} ORDER BY sort_order, id"), params).scalars())
        if item_id not in ids:
            return
        i = ids.index(item_id)
        j = i + (1 if direction > 0 else -1)
        if 0 <= j < len(ids):
            ids[i], ids[j] = ids[j], ids[i]
        for pos, iid in enumerate(ids, start=1):
            conn.execute(text(f"UPDATE {table} SET sort_order = :p WHERE id = :id"), {"p": pos * 10, "id": iid})


def rename_kind(kind_id: int, label: str, hidden: bool, db_path: DbTarget = None) -> None:
    label = (label or "").strip()
    if not label:
        raise ValueError("Название вида не может быть пустым")
    with _eng(db_path).begin() as conn:
        conn.execute(text("UPDATE kinds SET label = :l, hidden = :h WHERE id = :id"),
                     {"l": label, "h": int(bool(hidden)), "id": kind_id})


# ── Legacy migration (participations → achievements) ────────────────────────

_IDX_TO_ROW = {"ВАК": "I02.vak", "РИНЦ": "I02.rinc", "Белый список": "I02.white"}
_LEGACY_KIND = {"конференция": "doklad", "статья": "publication", "конкурс": "contest",
                "стипендия": "stipend", "грант": "grant"}


def migrate_legacy(conn) -> dict:  # noqa: ANN001
    """Copy not yet migrated participations into achievements (idempotent).
    конференция→Доклад (уровень не указан, очно), статья→Публикация (главный автор =
    первый добавивший, остальные — соавторы без долей), конкурс→п.3 (уровень не указан),
    стипендия→п.8 «иные», грант→«Грант» без подпункта. Nothing is deleted."""
    if "participations" not in inspect(conn).get_table_names():
        return {"migrated": 0, "articles_merged": 0, "multi_author": []}
    kinds = {r[0]: (int(r[1]), r[2]) for r in conn.execute(text("SELECT code, id, indicator_id FROM kinds"))}
    rows = {r[0]: int(r[1]) for r in conn.execute(text("SELECT code, id FROM indicator_rows"))}
    parts = _rows(conn.execute(text(
        """SELECT p.id AS pid, p.user_id, p.created_at, p.achievement_number, e.id AS event_id,
                  e.title, e.type, e.event_date, e.article_topic, e.indexing
           FROM participations p JOIN events e ON e.id = p.event_id
           WHERE NOT EXISTS (SELECT 1 FROM achievements a WHERE a.legacy_pid = p.id)
             AND NOT EXISTS (SELECT 1 FROM achievement_people ap WHERE ap.legacy_pid = p.id)
           ORDER BY e.id, p.created_at, p.id""")))
    migrated = merged = 0
    articles: dict[int, dict] = {}  # event_id → article achievement created in this run
    multi: list[int] = []
    names = {int(r[0]): r[1] for r in conn.execute(text("SELECT id, full_name FROM users"))}
    for p in parts:
        kcode = _LEGACY_KIND.get(p["type"])
        if kcode is None:
            continue
        kid, kind_ind = kinds[kcode]
        date_from = _iso(p["event_date"])
        base = {"kind_id": kid, "subpoint_id": None, "indicator_id": kind_ind, "row_id": None,
                "owner_id": p["user_id"], "title": p["title"], "date_from": date_from, "date_to": None,
                "number": _safe_number(p["achievement_number"]), "link": None, "topic": None,
                "ochno": 1, "owner_share": None, "externals": None, "event_id": p["event_id"],
                "legacy_pid": p["pid"], "created_at": str(p["created_at"] or _now())}
        details: dict[str, Any] = {"legacy": True}
        if kcode == "publication":
            art = articles.get(p["event_id"])
            if art is not None:
                art["n"] += 1
                conn.execute(text(
                    "INSERT INTO achievement_people (achievement_id, sort_order, user_id, name, share, legacy_pid) "
                    "VALUES (:a, :s, :u, :n, NULL, :l)"),
                    {"a": art["id"], "s": art["n"], "u": p["user_id"], "n": names.get(p["user_id"], ""),
                     "l": p["pid"]})
                if p["achievement_number"]:
                    art["details"].setdefault("coauthor_numbers", []).append(p["achievement_number"])
                art["details"]["legacy_coauthors"] = art["n"]
                conn.execute(text("UPDATE achievements SET details = :d WHERE id = :id"),
                             {"d": json.dumps(art["details"], ensure_ascii=False), "id": art["id"]})
                if art["id"] not in multi:
                    multi.append(art["id"])
                merged += 1
                continue
            row = _IDX_TO_ROW.get(p["indexing"] or "")
            base["row_id"] = rows[row] if row else None
            base["date_from"], base["date_to"] = f"{date_from[:4]}-01-01", f"{date_from[:4]}-12-31"
            base["event_id"] = None
            if (p["indexing"] or "") == "Без индексации":
                details["no_index"] = True
            details.update({"year": int(date_from[:4]), "legacy_indexing": p["indexing"] or "",
                            "legacy_topic": p["article_topic"] or "", "main_pos": 1})
        elif kcode == "stipend":
            base["row_id"] = rows["I08.other"]
            base["event_id"] = None
            details["ayear"] = f"{date_from[:4]}"
            base["date_to"] = f"{date_from[:4]}-12-31"
        elif kcode == "grant":
            base["event_id"] = None
        base["details"] = json.dumps(details, ensure_ascii=False)
        aid = _insert_returning_id(
            conn, "INSERT INTO achievements (" + ", ".join(base) + ") VALUES ("
            + ", ".join(":" + k for k in base) + ")", base)
        migrated += 1
        if kcode == "publication":
            articles[p["event_id"]] = {"id": aid, "n": 0, "details": details}
    return {"migrated": migrated, "articles_merged": merged, "multi_author": multi}


def _safe_number(v: Optional[str]) -> Optional[str]:
    try:
        return normalize_number(v)
    except ValueError:
        return (v or "").strip()[:NUMBER_MAX_LEN] or None


# ── CRUD ────────────────────────────────────────────────────────────────────


def resolve_target(kind: dict, subpoint_id: Optional[int], row_id: Optional[int]) -> tuple[Optional[int], Optional[int], str]:
    """(indicator_id, row_id, form) for a kind + chosen subpoint/level."""
    if kind["subpoints"] or kind["form"] == "grant":
        sp = next((s for s in kind["subpoints"] if s["id"] == subpoint_id), None)
        if sp is None:
            return None, None, "grant"
        return sp["indicator_id"], sp["row_id"] or row_id, sp["form"] or kind["form"]
    return kind["indicator_id"], row_id, kind["form"]


@db.cached
def find_duplicate_publication(title: str, year: Optional[int], doi: Optional[str],
                               exclude_id: Optional[int] = None, db_path: DbTarget = None) -> Optional[dict]:
    doi_n = normalize_doi(doi)
    tn = normalize_title(title)
    with _eng(db_path).connect() as conn:
        cands = _rows(conn.execute(text(
            "SELECT a.id, a.title, a.details, a.date_from, a.owner_id, u.full_name AS owner_name "
            "FROM achievements a JOIN kinds k ON k.id = a.kind_id LEFT JOIN users u ON u.id = a.owner_id "
            "WHERE k.form = 'publication' AND a.id <> :x"), {"x": exclude_id or 0}))
    for c in cands:
        d = _details(c["details"])
        if doi_n and normalize_doi(d.get("doi")) == doi_n:
            return c
        c_year = int(d.get("year") or str(c["date_from"] or "0")[:4] or 0)
        if tn and normalize_title(c["title"]) == tn and year and c_year == int(year):
            return c
    return None


def _details(raw: Any) -> dict:
    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}


def _num(v: Any) -> Optional[float]:
    if v in (None, ""):
        return None
    try:
        return float(str(v).replace(",", ".").replace("%", "").strip())
    except (TypeError, ValueError):
        raise ValueError("Доля участия — число от 0 до 100") from None


def validate_shares(owner_share: Any, coauthors: list[dict]) -> float:
    """Required shares: 0 < share ≤ 100 each, total ≤ 100. Returns total."""
    s = _num(owner_share)
    if s is None:
        raise ValueError("Укажите долю участия главного автора, %")
    if not 0 < s <= 100:
        raise ValueError("Доля участия должна быть больше 0 и не больше 100%")
    total = s
    for i, c in enumerate(coauthors, start=1):
        name = (c.get("name") or "").strip()
        if not name:
            raise ValueError(f"Соавтор {i}: укажите ФИО или выберите участника СНО")
        cs = _num(c.get("share"))
        if cs is None:
            raise ValueError(f"Соавтор {i} ({name}): укажите долю участия, %")
        if not 0 < cs <= 100:
            raise ValueError(f"Соавтор {i} ({name}): доля должна быть больше 0 и не больше 100%")
        total += cs
    if total > 100 + 1e-9:
        raise ValueError(f"Сумма долей {total:g}% больше 100% — исправьте доли.")
    return total


def save_achievement(data: dict, acting_user: dict, achievement_id: Optional[int] = None,
                     db_path: DbTarget = None) -> dict:
    """Create/update a record. data keys: kind_id, subpoint_id, row_id, owner_id, fields
    by FORMS (title, date_from, …), members [user ids], externals, owner_share,
    coauthors [{user_id, name, share}], main_pos, meeting_id.
    Members save only their own records (owner = themselves) of member kinds.
    Raises ValueError (validation) / DuplicateAchievement."""
    is_admin = acting_user.get("role") == "admin"
    kind = get_kind(int(data.get("kind_id") or 0), db_path)
    if kind is None:
        raise ValueError("Выберите вид достижения")
    if kind["admin_only"] and not is_admin:
        raise ValueError("Этот вид вносит только админ")
    old = get_achievement(achievement_id, db_path) if achievement_id else None
    if achievement_id and old is None:
        raise ValueError("Запись не найдена (возможно, удалена)")
    owner_id = data.get("owner_id") or None
    if not is_admin:
        owner_id = acting_user["id"]
        if old and old["owner_id"] != acting_user["id"]:
            raise ValueError("Можно менять только свои записи")
    elif not owner_id and not kind["admin_only"]:
        raise ValueError("Выберите участника")
    subpoint_id = data.get("subpoint_id") or None
    if kind["subpoints"] and not subpoint_id and not (old and old["subpoint_id"] is None and old["kind_id"] == kind["id"]):
        raise ValueError("Выберите подпункт")
    no_index = kind["form"] == "publication" and data.get("row_id") == NO_INDEX
    ind_id, row_id, form = resolve_target(kind, subpoint_id, None if no_index else (data.get("row_id") or None))
    if ind_id is not None and not row_id and not no_index and indicator_rows(ind_id, db_path, include_hidden=True):
        raise ValueError("Выберите уровень / подпункт")
    vals: dict[str, Any] = {}
    details: dict[str, Any] = dict(old["details"]) if old else {}
    if row_id:
        details.pop("legacy_indexing", None)
    if no_index:
        details["no_index"] = True
    else:
        details.pop("no_index", None)
    for key, label, ftype, required, extra in FORMS[form]:
        v = data.get(key)
        short = label.split(" (")[0]
        if ftype in ("people", "authors", "meeting"):
            continue
        if ftype == "ayear":
            vals["date_from"], vals["date_to"] = academic_year_range(v or "")
            details["ayear"] = str(v).strip()
            continue
        if ftype == "year":
            try:
                y = int(v)
            except (TypeError, ValueError):
                raise ValueError(f"Заполните поле «{short}»") from None
            if not 1990 <= y <= 2100:
                raise ValueError("Год публикации указан неверно")
            vals["date_from"], vals["date_to"] = f"{y}-01-01", f"{y}-12-31"
            details["year"] = y
            continue
        if ftype in ("date", "date_opt"):
            v = _iso(v)
        elif ftype == "check":
            v = 1 if v else 0
        elif isinstance(v, str):
            v = v.strip()
        if required and v in (None, ""):
            raise ValueError(f"Заполните поле «{short}»")
        if ftype == "select" and v and v not in extra["options"]:
            raise ValueError(f"Недопустимое значение поля «{short}»")
        if key == "number":
            v = normalize_number(v)
        if key in _COLUMNS:
            vals[key] = None if v == "" else v
        else:
            details[key] = "" if v is None else v
    if vals.get("date_to") and vals.get("date_from") and vals["date_to"] < vals["date_from"]:
        raise ValueError("Дата окончания раньше даты начала")
    if vals.get("date_to") and vals.get("date_to") == vals.get("date_from") and form != "stipend":
        vals["date_to"] = None
    title = vals.get("title") or ""
    people: list[dict] = []
    owner_share = None
    if form == "publication":
        coauthors = [c for c in (data.get("coauthors") or [])
                     if (c.get("name") or "").strip() or c.get("share") not in (None, "")]
        validate_shares(data.get("owner_share"), coauthors)
        owner_share = _num(data.get("owner_share"))
        people = [{"user_id": c.get("user_id") or None, "name": c["name"].strip(), "share": _num(c.get("share"))}
                  for c in coauthors]
        details["main_pos"] = min(max(int(data.get("main_pos") or 1), 1), len(people) + 1)
        dup = find_duplicate_publication(title, details.get("year"), details.get("doi"),
                                         exclude_id=achievement_id, db_path=db_path)
        if dup:
            raise DuplicateAchievement(
                f"Эта статья уже внесена: «{dup['title']}» — запись участника "
                f"{dup['owner_name'] or 'СНО'}. Если вы соавтор — попросите его добавить вас "
                f"в список соавторов (статья считается один раз).")
    elif any(f[2] == "people" for f in FORMS[form]):
        member_names = {u["id"]: u["full_name"] for u in db.list_users(active_only=False, db_path=db_path)}
        seen: set[int] = set()
        for u in data.get("members") or []:
            u = int(u)
            if u != (owner_id or 0) and u in member_names and u not in seen:
                seen.add(u)
                people.append({"user_id": u, "name": member_names[u], "share": None})
    externals = (data.get("externals") or "").strip() or None
    meeting_id = (data.get("meeting_id") or None) if form == "org" else None
    row = {
        "kind_id": kind["id"], "subpoint_id": subpoint_id if kind["subpoints"] else None,
        "indicator_id": ind_id, "row_id": row_id, "owner_id": owner_id,
        "title": title, "date_from": vals.get("date_from"), "date_to": vals.get("date_to"),
        "number": vals.get("number"), "link": vals.get("link"), "topic": vals.get("topic"),
        "ochno": vals.get("ochno", 1) if form == "doklad" else 1, "owner_share": owner_share,
        "externals": externals, "details": json.dumps(details, ensure_ascii=False),
        "meeting_id": meeting_id, "updated_at": _now(),
    }
    with _eng(db_path).begin() as conn:
        old_event = old["event_id"] if old else None
        row["event_id"] = (_event_for(conn, kind["code"], title, row["date_from"])
                           if kind["code"] in EVENT_KINDS else None)
        if old:
            conn.execute(text("UPDATE achievements SET " + ", ".join(f"{k} = :{k}" for k in row)
                              + " WHERE id = :id"), {**row, "id": achievement_id})
            # keep legacy markers of migrated co-authors so a re-run never re-imports them
            conn.execute(text("DELETE FROM achievement_people WHERE achievement_id = :a AND legacy_pid IS NULL"),
                         {"a": achievement_id})
            conn.execute(text("UPDATE achievement_people SET sort_order = -1 WHERE achievement_id = :a"),
                         {"a": achievement_id})
            legacy_left = _rows(conn.execute(text(
                "SELECT id, user_id FROM achievement_people WHERE achievement_id = :a"), {"a": achievement_id}))
            aid = achievement_id
        else:
            row.update({"created_at": _now(), "created_by": acting_user.get("id")})
            aid = _insert_returning_id(
                conn, "INSERT INTO achievements (" + ", ".join(row) + ") VALUES ("
                + ", ".join(":" + k for k in row) + ")", row)
            legacy_left = []
        reuse = {r["user_id"]: r["id"] for r in legacy_left if r["user_id"]}
        for i, p in enumerate(people, start=1):
            rid = reuse.pop(p["user_id"], None) if p["user_id"] else None
            if rid:
                conn.execute(text("UPDATE achievement_people SET sort_order = :s, name = :n, share = :sh "
                                  "WHERE id = :id"), {"s": i, "n": p["name"], "sh": p["share"], "id": rid})
            else:
                conn.execute(text("INSERT INTO achievement_people (achievement_id, sort_order, user_id, name, share) "
                                  "VALUES (:a, :s, :u, :n, :sh)"),
                             {"a": aid, "s": i, "u": p["user_id"], "n": p["name"], "sh": p["share"]})
        # legacy co-author rows removed by the editor: detach (keeps the marker, hides the person)
        for rid in reuse.values():
            conn.execute(text("UPDATE achievement_people SET name = '' WHERE id = :id"), {"id": rid})
        for r in legacy_left:
            if not r["user_id"]:
                conn.execute(text("UPDATE achievement_people SET name = '' WHERE id = :id AND sort_order = -1"),
                             {"id": r["id"]})
        if old_event and old_event != row["event_id"]:
            db._delete_orphans(conn, [int(old_event)])
    rec = get_achievement(aid, db_path)
    return {"id": aid, "issues": rec["issues"] if rec else []}


def _event_for(conn, kind_code: str, title: str, date_from: Optional[str]) -> Optional[int]:  # noqa: ANN001
    if not title or not date_from:
        return None
    key = {"tn": normalize_title(title), "t": EVENT_KINDS[kind_code], "d": date_from}
    eid = conn.execute(text("SELECT id FROM events WHERE title_norm = :tn AND type = :t AND event_date = :d"),
                       key).scalar()
    if eid:
        return int(eid)
    return _insert_returning_id(
        conn, "INSERT INTO events (title, title_norm, type, event_date, created_at) "
              "VALUES (:title, :tn, :t, :d, :now)", {**key, "title": title.strip(), "now": _now()})


def delete_achievement(achievement_id: int, owner_id: Optional[int] = None, db_path: DbTarget = None) -> bool:
    """Delete a record (owner_id → only own). Its legacy participation(s) go too, and the
    event is removed when nothing references it any more (meetings keep their row)."""
    with _eng(db_path).begin() as conn:
        a = _one(conn.execute(text("SELECT id, owner_id, event_id, legacy_pid FROM achievements WHERE id = :id"),
                              {"id": achievement_id}))
        if a is None or (owner_id is not None and a["owner_id"] != owner_id):
            return False
        legacy = [a["legacy_pid"]] if a["legacy_pid"] else []
        legacy += list(conn.execute(text(
            "SELECT legacy_pid FROM achievement_people WHERE achievement_id = :id AND legacy_pid IS NOT NULL"),
            {"id": achievement_id}).scalars())
        conn.execute(text("DELETE FROM achievement_people WHERE achievement_id = :id"), {"id": achievement_id})
        conn.execute(text("DELETE FROM achievements WHERE id = :id"), {"id": achievement_id})
        events = {int(a["event_id"])} if a["event_id"] else set()
        for pid in legacy:
            ev = conn.execute(text("SELECT event_id FROM participations WHERE id = :p"), {"p": pid}).scalar()
            if ev:
                events.add(int(ev))
            conn.execute(text("DELETE FROM participations WHERE id = :p"), {"p": pid})
        if events:
            db._delete_orphans(conn, sorted(events))
        return True


_SELECT = """
    SELECT a.*, k.code AS kind_code, k.label AS kind_label, k.form AS kind_form, k.admin_only,
           i.label AS indicator_label, i.code AS indicator_code, i.dimension,
           r.label AS row_label, r.code AS row_code, s.label AS subpoint_label, s.form AS subpoint_form,
           u.full_name AS owner_name, u.login AS owner_login
    FROM achievements a
    JOIN kinds k ON k.id = a.kind_id
    LEFT JOIN indicators i ON i.id = a.indicator_id
    LEFT JOIN indicator_rows r ON r.id = a.row_id
    LEFT JOIN kind_subpoints s ON s.id = a.subpoint_id
    LEFT JOIN users u ON u.id = a.owner_id
"""


def _decorate(conn, recs: list[dict]) -> list[dict]:  # noqa: ANN001
    if not recs:
        return recs
    ids = [r["id"] for r in recs]
    people: dict[int, list[dict]] = {}
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        names = [f"a{j}" for j in range(len(chunk))]
        for p in _rows(conn.execute(text(
                "SELECT * FROM achievement_people WHERE sort_order >= 0 AND achievement_id IN ("
                + ", ".join(":" + n for n in names) + ") ORDER BY sort_order, id"), dict(zip(names, chunk)))):
            people.setdefault(p["achievement_id"], []).append(p)
    has_rows = {r[0] for r in conn.execute(text("SELECT DISTINCT indicator_id FROM indicator_rows"))}
    for r in recs:
        r["people"] = people.get(r["id"], [])
        r["details"] = _details(r["details"])
        r["date_from"] = _iso(r["date_from"])
        r["date_to"] = _iso(r["date_to"])
        if r["kind_form"] == "grant":
            r["form"] = (r["subpoint_form"] or "grant") if r["subpoint_id"] else "grant"
        else:
            r["form"] = r["kind_form"]
        r["indicator_has_rows"] = r["indicator_id"] in has_rows
        r["counted"] = is_counted(r)
        r["issues"] = record_issues(r)
        if r["details"].get("no_index") and not r["row_id"]:
            r["row_label"] = NO_INDEX_LABEL
    return recs


@db.cached
def get_achievement(achievement_id: Optional[int], db_path: DbTarget = None) -> Optional[dict]:
    if not achievement_id:
        return None
    with _eng(db_path).connect() as conn:
        rec = _one(conn.execute(text(_SELECT + " WHERE a.id = :id"), {"id": achievement_id}))
        return _decorate(conn, [rec])[0] if rec else None


def year_cond(year: Optional[int], alias: str = "a") -> tuple[str, dict]:
    """A record belongs to a year when its period intersects it (stipend 2024-2025 and
    2025-2026 both count for 2025, as in the example report)."""
    if not year:
        return "", {}
    return (f" AND {alias}.date_from <= :y_to AND COALESCE({alias}.date_to, {alias}.date_from) >= :y_from",
            {"y_from": f"{int(year):04d}-01-01", "y_to": f"{int(year):04d}-12-31"})


@db.cached
def list_achievements(owner_id: Optional[int] = None, year: Optional[int] = None,
                      indicator_id: Optional[int] = None, row_id: Optional[int] = None,
                      kind_id: Optional[int] = None, without_number: bool = False,
                      needs_fill: bool = False, db_path: DbTarget = None) -> list[dict]:
    sql = _SELECT + " WHERE 1=1"
    params: dict[str, Any] = {}
    if owner_id is not None:
        sql += " AND a.owner_id = :o"
        params["o"] = owner_id
    cond, p2 = year_cond(year)
    sql += cond
    params.update(p2)
    if indicator_id is not None:
        sql += " AND a.indicator_id = :i"
        params["i"] = indicator_id
    if row_id is not None:
        sql += " AND a.row_id = :r"
        params["r"] = row_id
    if kind_id is not None:
        sql += " AND a.kind_id = :k"
        params["k"] = kind_id
    if without_number:
        sql += " AND (a.number IS NULL OR a.number = '')"
    sql += " ORDER BY a.date_from DESC, a.id DESC"
    with _eng(db_path).connect() as conn:
        recs = _decorate(conn, _rows(conn.execute(text(sql), params)))
    if needs_fill:
        recs = [r for r in recs if r["issues"]]
    return recs


def is_counted(r: dict) -> bool:
    """Counting rule: 1 record = 1 unit, if its indicator/sub-row is known; заочные доклады
    are stored but not reported."""
    if r["indicator_id"] is None:
        return False
    if r.get("indicator_has_rows") and r["row_id"] is None:
        return False
    if r["form"] == "doklad" and not r["ochno"]:
        return False
    return True


def record_issues(r: dict) -> list[str]:
    """What the owner/admin still has to fill («заполните …»)."""
    out = []
    d = r["details"]
    if r["kind_form"] == "grant" and not r["subpoint_id"]:
        out.append("выберите подпункт гранта")
    elif r["indicator_id"] is not None and r.get("indicator_has_rows") and r["row_id"] is None \
            and not d.get("no_index"):
        if r["form"] == "publication":
            out.append("выберите индексацию"
                       + (" (было «Без индексации»)" if d.get("legacy_indexing") == "Без индексации" else ""))
        else:
            out.append("укажите уровень" if r["dimension"] == "level" else "выберите подпункт")
    if r["form"] == "publication":
        if r["owner_share"] is None or any(p["share"] is None for p in r["people"]):
            out.append("заполните долю")
        if not d.get("journal") and not d.get("bib"):
            out.append("заполните журнал")
        if d.get("legacy_coauthors") and r["owner_share"] is None:
            out.append("проверьте главного автора (перенесено: первый добавивший)")
    if r["form"] == "doklad" and not (r["topic"] or "").strip():
        out.append("заполните тему доклада")
    if r["form"] == "stipend" and not re.match(r"^\d{4}-\d{4}$", str(d.get("ayear") or "")):
        out.append("укажите учебный год")
    return out


# ── Summaries & stats ───────────────────────────────────────────────────────


@db.cached
def member_year_summary(owner_id: int, year: int, db_path: DbTarget = None) -> list[tuple[str, int]]:
    recs = list_achievements(owner_id=owner_id, year=year, db_path=db_path)
    counts: dict[str, int] = {}
    for r in recs:
        counts[r["kind_label"]] = counts.get(r["kind_label"], 0) + 1
    order = [k["label"] for k in list_kinds(db_path, admin=True, include_hidden=True)]
    return sorted(counts.items(), key=lambda kv: order.index(kv[0]) if kv[0] in order else 99)


@db.cached
def stats_by_person(year: Optional[int] = None, db_path: DbTarget = None) -> list[dict]:
    """Per member: total records, counted in report, with number, by kind."""
    users = db.list_users(active_only=False, db_path=db_path)
    recs = list_achievements(year=year, db_path=db_path)
    out = {u["id"]: {"user_id": u["id"], "full_name": u["full_name"], "login": u["login"], "total": 0,
                     "counted": 0, "with_number": 0, "by_kind": {}} for u in users}
    for r in recs:
        o = out.get(r["owner_id"])
        if o is None:
            continue
        o["total"] += 1
        o["counted"] += int(r["counted"])
        o["with_number"] += int(bool(r["number"]))
        o["by_kind"][r["kind_label"]] = o["by_kind"].get(r["kind_label"], 0) + 1
    rows = [o for o in out.values() if o["total"]]
    rows.sort(key=lambda o: (-o["total"], o["full_name"]))
    return rows


@db.cached
def events_overview(year: Optional[int] = None, db_path: DbTarget = None,
                    with_names: bool = False) -> list[dict]:
    """Event-level list: records grouped by kind + title + date.

    Members get no names; with_names=True (admin only) adds "names" (members + externals).
    """
    recs = list_achievements(year=year, db_path=db_path)
    groups: dict[tuple, dict] = {}
    for r in recs:
        key = (r["kind_label"], normalize_title(r["title"]), r["date_from"])
        g = groups.setdefault(key, {"title": r["title"], "kind": r["kind_label"],
                                    "date": r["date_from"], "records": 0, "people": set(),
                                    "names": []})
        g["records"] += 1
        for nm in people_names(r):
            if nm not in g["names"]:
                g["names"].append(nm)
        if r["owner_id"]:
            g["people"].add(r["owner_id"])
        for p in r["people"]:
            if p["user_id"]:
                g["people"].add(p["user_id"])
    # "people" = distinct members; "names" (admin only) also lists external co-authors
    out = [{**g, "people": len(g["people"])} for g in groups.values()]
    if not with_names:
        for g in out:
            g.pop("names", None)
    out.sort(key=lambda g: (g["date"] or "", g["title"].lower()), reverse=True)
    return out


@db.cached
def available_years(db_path: DbTarget = None) -> list[int]:
    years: set[int] = set()
    with _eng(db_path).connect() as conn:
        for a, b in conn.execute(text("SELECT date_from, date_to FROM achievements WHERE date_from IS NOT NULL")):
            y1 = int(str(a)[:4])
            y2 = int(str(b)[:4]) if b else y1
            years.update(range(y1, min(y2, y1 + 3) + 1))
    return sorted(years, reverse=True)


# ── Report lines ────────────────────────────────────────────────────────────


def _period(r: dict) -> str:
    a, b = fmt_date(r["date_from"]), fmt_date(r["date_to"])
    return f"{a}-{b}" if b and b != a else a


def people_names(r: dict, with_owner: bool = True) -> list[str]:
    out = [r["owner_name"]] if with_owner and r.get("owner_name") else []
    out += [p["name"] for p in r["people"] if p["name"]]
    if r.get("externals"):
        out += [x.strip() for x in re.split(r"[;,\n]", r["externals"]) if x.strip()]
    return out


def _join(parts: list[str]) -> str:
    return ", ".join(str(p).strip().rstrip(",") for p in parts if p and str(p).strip())


def publication_authors(r: dict) -> list[tuple[str, Optional[float]]]:
    """Authors in order with shares; the main author stands at details.main_pos."""
    co = [(p["name"], p["share"]) for p in r["people"]]
    pos = min(max(int(r["details"].get("main_pos") or 1), 1), len(co) + 1)
    main = (r.get("owner_name") or "", r["owner_share"])
    return co[:pos - 1] + [main] + co[pos - 1:]


def shares_text(r: dict) -> str:
    return "доля участия: " + ", ".join(_share_str(s) for _, s in publication_authors(r))


def publication_line(r: dict) -> str:
    d = r["details"]
    num = r["number"] or ""
    share = shares_text(r)
    share = share[0].upper() + share[1:]
    if (d.get("bib") or "").strip():
        line = d["bib"].strip()
        if "доля участия" not in line.lower():
            line = line.rstrip(" .") + ". " + share + "."
        if num and num not in line:
            line += " " + num
        return line
    authors = (d.get("authors_text") or "").strip() or ", ".join(n for n, _ in publication_authors(r) if n)
    parts = [authors.rstrip(". "), (r["title"] or "").strip().rstrip(". ")]
    if d.get("journal"):
        parts.append(str(d["journal"]).strip().rstrip(". "))
    line = ". ".join(p for p in parts if p) + f". ({d.get('year') or (r['date_from'] or '')[:4]})."
    if r["link"]:
        line += " " + r["link"].strip()
    if d.get("doi"):
        line += f" DOI: {str(d['doi']).strip()}."
    line += " " + share + "."
    if num:
        line += " " + num
    return line


def record_line(r: dict, sno_name: str = "") -> str:
    """One detail line for the report (every form except doklad, which is grouped)."""
    d, f, num = r["details"], r["form"], r["number"] or ""
    names = people_names(r)
    if f == "publication":
        return publication_line(r)
    if f == "stipend":
        ay = d.get("ayear") or _period(r)
        return _join([f"{r['title']} {ay} гг." if re.match(r"^\d{4}-\d{4}$", str(ay)) else f"{r['title']}, {ay}",
                      *names, num])
    if f == "work":
        return _join([r["title"], fmt_date(r["date_from"]), *names,
                      f"Тема: «{r['topic']}»" if r["topic"] else "", num])
    if f == "grant_app":
        return _join([r["title"], f"проект «{d.get('project')}»" if d.get("project") else "",
                      fmt_date(r["date_from"]), f"заявка {d.get('status')}" if d.get("status") else "",
                      *names, num])
    if f == "ip":
        doc = (f"№ {d.get('doc_number')} от {fmt_date(r['date_from'])}" if d.get("doc_number")
               else fmt_date(r["date_from"]))
        return _join([f"«{r['title']}»", doc, *names, num])
    if f == "exchange":
        return _join([r["title"], d.get("org") or "", _period(r), *names, num])
    if f == "org":
        order = ""
        if d.get("order_no") or d.get("order_date"):
            order = ("приказ" + (f" от {fmt_date(d.get('order_date'))}" if d.get("order_date") else "")
                     + (f", №{d['order_no']}" if d.get("order_no") else ""))
        return _join([fmt_date(r["date_from"]), r["title"], order, d.get("order_subject") or ""])
    if f == "sno_contest":
        return _join([r["title"], _period(r), *names, d.get("result") or "",
                      f"СНО «{sno_name}»" if sno_name else "", num])
    if f == "agreement":
        return _join([r["title"], d.get("agreement") or "", d.get("subject") or ""])
    if f == "funded":
        return _join([f"{d.get('source')}: {r['title']}" if d.get("source") else r["title"],
                      d.get("contract") or "", _period(r), num])
    if f == "expo":
        return _join([r["title"], _period(r), f"экспонат: «{d['exhibit']}»" if d.get("exhibit") else "",
                      *names, num])
    if f == "contest":
        res = (d.get("result") or "").strip()
        return _join([r["title"], _period(r), *names, num, res if res.lower() != "участие" else ""])
    return _join([r["title"], _period(r), *names, num])


def doklad_items(recs: list[dict]) -> list[tuple]:
    """Grouped by event: («event», "<Мероприятие>, DD.MM.YYYY г.") followed by numbered
    («name», ФИО, " доклад на тему: «…», Р-Н-…") lines."""
    groups: dict[tuple, list[dict]] = {}
    for r in recs:
        groups.setdefault((r["date_from"] or "", normalize_title(r["title"])), []).append(r)
    items: list[tuple] = []
    for key in sorted(groups):
        g = groups[key]
        items.append(("event", f"{g[0]['title'].strip()}, {fmt_date(g[0]['date_from'])} г."))
        for r in sorted(g, key=lambda x: ((x["owner_name"] or "").lower(), x["id"])):
            rest = f" доклад на тему: «{(r['topic'] or '').strip()}»"
            if r["number"]:
                rest += f", {r['number']}"
            items.append(("name", r["owner_name"] or "", rest))
    return items


def _items(recs: list[dict], sno_name: str) -> list[tuple]:
    if recs and recs[0]["form"] == "doklad":
        return doklad_items(recs)
    return [("line", record_line(r, sno_name)) for r in recs]


@db.cached
def report_data(year: int, db_path: DbTarget = None) -> dict:
    """Everything for the annual report: indicators with counts and detail items, warnings."""
    sno_name = db.get_report_settings(db_path=db_path)["sno_name"]
    recs = list_achievements(year=year, db_path=db_path)
    cat = catalog(db_path, include_hidden=True)
    by_row: dict[int, list[dict]] = {}
    by_ind: dict[int, list[dict]] = {}
    for r in recs:
        if not r["counted"]:
            continue
        if r["row_id"]:
            by_row.setdefault(r["row_id"], []).append(r)
        else:
            by_ind.setdefault(r["indicator_id"], []).append(r)
    key = lambda x: (x["date_from"] or "", x["id"])  # noqa: E731
    indicators = []
    for ind in cat:
        rows = []
        for row in ind["rows"]:
            rr = sorted(by_row.get(row["id"], []), key=key)
            if row["hidden"] and not rr:
                continue
            rows.append({"id": row["id"], "label": row["label"], "count": len(rr), "records": rr,
                         "items": _items(rr, sno_name)})
        own = sorted(by_ind.get(ind["id"], []), key=key)
        total = sum(r["count"] for r in rows) + len(own)
        if ind["hidden"] and not total:
            continue
        indicators.append({"id": ind["id"], "code": ind["code"], "label": ind["label"], "count": total,
                           "rows": rows, "records": own, "items": _items(own, sno_name)})
    warnings = [{"id": r["id"], "owner": r["owner_name"] or "СНО", "kind": r["kind_label"],
                 "title": r["title"], "date": fmt_date(r["date_from"]),
                 "issues": ", ".join(r["issues"]), "counted": r["counted"]}
                for r in recs if r["issues"] and not r["details"].get("no_index")]
    zaochno = sum(1 for r in recs if r["form"] == "doklad" and not r["ochno"])
    return {"year": year, "sno_name": sno_name, "indicators": indicators, "warnings": warnings,
            "zaochno": zaochno, "total": sum(i["count"] for i in indicators), "records": recs}


# ── Conclusion text per year ────────────────────────────────────────────────


@db.cached
def get_conclusion(year: int, db_path: DbTarget = None) -> str:
    return db.get_app_setting(f"annual_conclusion_{int(year)}", "", db_path=db_path) or ""


def set_conclusion(year: int, value: str, db_path: DbTarget = None) -> None:
    db.set_app_setting(f"annual_conclusion_{int(year)}", (value or "").strip(), db_path=db_path)
