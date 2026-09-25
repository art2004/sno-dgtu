"""Smoke-test: admin seed, member create, event add, dedup, cascade, password change,
meetings CRUD, report settings, .docx/.xlsx report generation, year-filtered stats,
signed «remember me» cookie tokens (expiry, tamper, password change, deletion),
members import from Excel.

По умолчанию проверяет временную SQLite-базу (рабочая data/sno.db не трогается).

Проверка на Postgres (ОСТОРОЖНО: таблицы в этой базе будут удалены!):
    SMOKE_DATABASE_URL=postgresql://user:pass@localhost/sno_test python smoke_test.py
Используйте отдельную тестовую базу, не продакшен.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from sqlalchemy import text  # noqa: E402

import db  # noqa: E402
from auth import authenticate, change_password, hash_password, verify_password  # noqa: E402


def _reset_pg(target: str) -> None:
    with db.get_engine(target).begin() as conn:
        conn.execute(text(
            "DROP TABLE IF EXISTS participations, events, users, meetings, settings CASCADE"
        ))


def run(target, expect_admin_password: str) -> None:  # noqa: ANN001
    db.init_db(db_path=target, seed_admin=True)
    print(f"  backend: {db.backend_name(target)}")

    admin_login = os.environ.get("ADMIN_LOGIN") or "admin"
    admin = db.get_user_by_login(admin_login, db_path=target)
    assert admin is not None, "admin not seeded"
    assert admin["role"] == "admin"
    assert admin["full_name"] == "Админ СНО"
    assert verify_password(expect_admin_password, admin["password_hash"])
    assert not verify_password("wrong", admin["password_hash"])
    assert db.ensure_admin(target) == "exists"

    uid1 = db.create_user("ivanov", "pass1", "Иванов И.И.", db_path=target)
    uid2 = db.create_user("petrov", "pass2", "Петров П.П.", db_path=target)
    uid3 = db.create_user("sidorov", "pass3", "Сидоров С.С.", db_path=target)

    # Duplicate login → DuplicateError
    try:
        db.create_user("ivanov", "x", "Дубль", db_path=target)
        raise AssertionError("expected DuplicateError on duplicate login")
    except db.DuplicateError:
        pass
    try:
        db.update_user(uid2, login="ivanov", db_path=target)
        raise AssertionError("expected DuplicateError on login rename")
    except db.DuplicateError:
        pass

    title_a = "  УМНИК   2026  "
    title_b = "умник 2026"  # same after normalize
    etype = "грант"
    edate = "2026-03-15"

    pid1, eid1 = db.add_participation(uid1, title_a, etype, edate, db_path=target)
    pid2, eid2 = db.add_participation(uid2, title_b, etype, edate, db_path=target)
    assert eid1 == eid2, f"dedup failed: {eid1} != {eid2}"
    assert db.count_events(db_path=target) == 1, "expected exactly one event"

    _, eid3 = db.add_participation(uid1, title_b, "конкурс", edate, db_path=target)
    assert eid3 != eid1
    assert db.count_events(db_path=target) == 2
    db.add_participation(uid3, "Наука ДГТУ", "конференция", "2026-04-01", db_path=target)

    try:
        db.add_participation(uid1, title_b, etype, edate, db_path=target)
        raise AssertionError("expected DuplicateError on duplicate participation")
    except db.DuplicateError:
        pass

    rows1 = db.list_participations_for_user(uid1, db_path=target)
    assert len(rows1) == 2
    assert rows1[0]["title"] and rows1[0]["joined_at"][:10]

    by_person = db.stats_by_person(db_path=target)
    assert len(by_person) == 3
    top = by_person[0]
    assert top["full_name"] == "Иванов И.И." and top["total"] == 2
    assert top["grants"] == 1 and top["contests"] == 1 and top["conferences"] == 0
    by_type = {r["type"]: r for r in db.stats_by_type(db_path=target)}
    assert by_type["грант"]["participations_count"] == 2
    assert by_type["грант"]["events_count"] == 1
    by_event = db.stats_by_event(db_path=target)
    assert len(by_event) == 3

    allp = db.list_all_participations(event_type="грант", db_path=target)
    assert len(allp) == 2
    allp = db.list_all_participations(date_from="2026-03-20", db_path=target)
    assert len(allp) == 1
    assert len(db.list_users(role="member", db_path=target)) == 3

    # Delete own participation only
    assert not db.delete_participation(pid2, user_id=uid1, db_path=target)
    assert db.delete_participation(pid2, user_id=uid2, db_path=target)

    # Deactivate → authenticate fails; active flag respected
    db.update_user(uid3, active=False, db_path=target)
    assert len(db.stats_by_person(db_path=target)) == 2

    # Cascade delete user → participations gone
    db.delete_user(uid1, db_path=target)
    assert db.list_participations_for_user(uid1, db_path=target) == []
    assert len(db.list_all_participations(db_path=target)) == 1  # only sidorov's

    # Password change
    ok, msg = change_password(uid2, "wrong", "newpass123", "newpass123", db_path=target)
    assert not ok, msg
    ok, msg = change_password(uid2, "pass2", "short", "short", db_path=target)
    assert not ok, msg
    ok, msg = change_password(uid2, "pass2", "newpass123", "newpass124", db_path=target)
    assert not ok, msg
    ok, msg = change_password(uid2, "pass2", "newpass123", "newpass123", db_path=target)
    assert ok, msg
    u2 = db.get_user_by_id(uid2, db_path=target)
    assert verify_password("newpass123", u2["password_hash"])

    run_meetings_and_report(target)
    run_year_stats(target)
    run_new_types_and_achievements(target)
    run_activity_kinds(target)
    run_schema_upgrade(target)
    run_auth_cookie(target)
    run_members_import(target)

    assert db.normalize_title("  Foo   BAR ") == "foo bar"
    assert verify_password("x", hash_password("x"))
    print(f"  events={db.count_events(db_path=target)}, shared grant eid={eid1}")


def run_meetings_and_report(target) -> None:  # noqa: ANN001
    import io

    import docx
    import openpyxl

    import report
    from ru_text import PARTICIPATION_FORMS, member_year_summary, with_count

    assert db.list_meetings(2025, db_path=target) == []
    m2 = db.add_meeting("2025-02-14", "13:30", "Главный корпус, 161 ауд.", "Тема два",
                        db.join_formats(["Собрание"]), db_path=target)
    m1 = db.add_meeting("2025-01-16", "", "Онлайн", "Тема один",
                        db.join_formats(["Форсайт-сессия", "Собрание"], "Батл"), db_path=target)
    m3 = db.add_meeting("2025-03-07", "9:05", "Главный корпус, 031 ауд.", "Тема три",
                        "Собрание", db_path=target)
    m_other = db.add_meeting("2024-12-26", "13:30", "Где-то", "Прошлый год", "Собрание",
                             db_path=target)
    try:
        db.add_meeting("2025-04-01", "25:99", "x", "x", "x", db_path=target)
        raise AssertionError("expected ValueError on bad time")
    except ValueError:
        pass

    ms = db.list_meetings(2025, db_path=target)
    assert [m["id"] for m in ms] == [m1, m2, m3], ms
    assert [m["number"] for m in ms] == [1, 2, 3]
    assert ms[0]["meeting_time"] == "13:30"  # default when empty
    assert ms[2]["meeting_time"] == "09:05"
    assert ms[0]["format"] == "Форсайт-сессия, Собрание, Батл"
    assert db.split_formats(ms[0]["format"]) == (["Форсайт-сессия", "Собрание"], "Батл")
    assert ms[0]["meeting_date"].isoformat() == "2025-01-16"
    assert db.count_meetings(2025, db_path=target) == 3
    assert db.count_meetings(db_path=target) == 4
    assert db.recent_locations(db_path=target)[0] == "Главный корпус, 031 ауд."

    assert db.update_meeting(m3, "2025-03-08", "14:00", "Главный корпус, 031 ауд.",
                             "Тема три (изм.)", "Круглый стол", db_path=target)
    got = db.get_meeting(m3, db_path=target)
    assert got["topic"] == "Тема три (изм.)" and got["meeting_time"] == "14:00"
    assert got["meeting_date"].isoformat() == "2025-03-08"
    assert db.delete_meeting(m_other, db_path=target)
    assert not db.delete_meeting(m_other, db_path=target)
    assert db.get_meeting(m_other, db_path=target) is None

    # Settings: defaults, then save/load (upsert twice)
    s = db.get_report_settings(db_path=target)
    assert s["sno_name"] == db.DEFAULT_SNO_NAME
    assert s["appendix_label"] == "Приложение Е"
    assert [x["position"] for x in s["signatories"]] == [
        "Начальник Управления НИРО", "Декан факультета «…»", "Научный наставник"]
    db.save_report_settings("Черновик", "Приложение Ж", [], db_path=target)
    sigs = [
        {"position": "Начальник Управления НИРО", "name": "Вершинина А.В."},
        {"position": "", "name": ""},  # пустая строка из data_editor отбрасывается
        {"position": "Научный наставник", "name": "Одабашян М.Ю."},
    ]
    db.save_report_settings("«Сельское хозяйство»", "Приложение Е", sigs, db_path=target)
    s = db.get_report_settings(db_path=target)
    assert s["sno_name"] == "Сельское хозяйство", s
    assert s["appendix_label"] == "Приложение Е"
    assert len(s["signatories"]) == 2

    # DOCX report
    ms = db.list_meetings(2025, db_path=target)
    data = report.build_meetings_docx(ms, 2025, s["sno_name"], s["appendix_label"], s["signatories"])
    doc = docx.Document(io.BytesIO(data))
    table = doc.tables[0]
    assert len(table.rows) == len(ms) + 1, len(table.rows)
    assert table.rows[0].cells[0].text.startswith("№")
    assert [r.cells[0].text for r in table.rows[1:]] == ["1.", "2.", "3."]
    assert table.rows[1].cells[1].text == "16.01.2025,\n13:30"
    assert table.rows[1].cells[4].text == "Форсайт-сессия,\nСобрание,\nБатл"
    assert table.rows[3].cells[3].text == "Тема три (изм.)"
    texts = [p.text for p in doc.paragraphs]
    title = next(t for t in texts if t.startswith("Отчет о проведенных заседаниях"))
    assert "2025" in title and "«Сельское хозяйство»" in title, title
    assert texts[0] == "Приложение Е"
    sig_lines = [t for t in texts if "_______________/" in t]
    assert sig_lines == [
        "Начальник Управления НИРО _______________/Вершинина А.В.",
        "Научный наставник _______________/Одабашян М.Ю.",
    ], sig_lines
    assert not any("{" in t for t in texts), texts
    assert doc.sections[0].orientation == 1  # landscape

    empty = docx.Document(io.BytesIO(report.build_meetings_docx([], 2030, "X", "Приложение Е", [])))
    assert len(empty.tables[0].rows) == 1
    assert not any("_______________/" in p.text for p in empty.paragraphs)

    # XLSX export
    df, dt = db.year_range(2026)
    parts = db.list_all_participations(date_from=df, date_to=dt, db_path=target)
    wb = openpyxl.load_workbook(io.BytesIO(report.build_year_xlsx(parts, ms)))
    assert wb.sheetnames == ["Участия", "Заседания"]
    assert wb["Участия"].max_row == len(parts) + 1
    assert wb["Заседания"].max_row == len(ms) + 1

    assert with_count(21, PARTICIPATION_FORMS) == "21 участие"
    assert with_count(12, PARTICIPATION_FORMS) == "12 участий"
    assert with_count(3, PARTICIPATION_FORMS) == "3 участия"
    assert "2 конференции, 1 грант, 5 конкурсов" in member_year_summary(
        2026, {"total": 8, "конференция": 2, "грант": 1, "конкурс": 5})
    assert "пока нет" in member_year_summary(2026, {"total": 0})
    print(f"  meetings/report OK: {len(ms)} meetings, docx {len(data)} bytes")


def run_year_stats(target) -> None:  # noqa: ANN001
    uid = db.create_user("yeartest", "pass12345", "Годов Г.Г.", db_path=target)
    db.add_participation(uid, "Старый грант", "грант", "2024-05-10", db_path=target)
    db.add_participation(uid, "Зимняя конференция", "конференция", "2025-01-20", db_path=target)
    db.add_participation(uid, "Весенний конкурс", "конкурс", "2025-03-03", db_path=target)
    db.add_participation(uid, "Ещё конференция", "конференция", "2025-03-30", db_path=target)

    assert db.count_participations(2025, db_path=target) == 3
    assert db.count_events(2025, db_path=target) == 3
    assert db.count_events(2024, db_path=target) == 1
    months = db.participations_by_month(2025, db_path=target)
    assert len(months) == 12 and months[0] == 1 and months[2] == 2 and sum(months) == 3
    assert sum(db.participations_by_month(None, db_path=target)) == db.count_participations(
        db_path=target)
    per = {r["login"]: r for r in db.stats_by_person(2025, db_path=target)}
    assert per["yeartest"]["total"] == 3 and per["yeartest"]["conferences"] == 2
    assert per["yeartest"]["grants"] == 0 and per["yeartest"]["contests"] == 1
    assert all(r["total"] == 0 for k, r in per.items() if k != "yeartest")  # others: 2026 only
    per_all = {r["login"]: r for r in db.stats_by_person(db_path=target)}
    assert per_all["yeartest"]["total"] == 4
    by_type = {r["type"]: r for r in db.stats_by_type(2025, db_path=target)}
    assert by_type["конференция"]["participations_count"] == 2 and "грант" not in by_type
    assert len(db.stats_by_event(2024, db_path=target)) == 1
    summ = db.user_year_summary(uid, 2025, db_path=target)
    assert summ == {"грант": 0, "конференция": 2, "конкурс": 1, "стипендия": 0, "статья": 0,
                    "total": 3}, summ
    years = db.available_years(db_path=target)
    assert {2024, 2025, 2026} <= set(years) and years == sorted(years, reverse=True)
    print(f"  year stats OK: years={years}, 2025 by month={months}")


def run_new_types_and_achievements(target) -> None:  # noqa: ANN001
    from ru_text import member_year_summary

    assert db.EVENT_TYPES[:3] == ("грант", "конференция", "конкурс")
    assert "стипендия" in db.EVENT_TYPES and "статья" in db.EVENT_TYPES
    a = db.create_user("author1", "pass12345", "Авторова А.А.", db_path=target)
    b = db.create_user("author2", "pass12345", "Бэ Б.Б.", db_path=target)

    r1 = db.add_participation_ex(a, "Нейросети в АПК", "статья", "2025-06-01",
                                 article_topic="ИИ", indexing="ВАК",
                                 achievement_number="  ACH-001  ", db_path=target)
    assert r1["indexing_conflict"] is None
    # co-author, same article (dedup title+type+date), different indexing → first kept
    r2 = db.add_participation_ex(b, "нейросети  в апк", "статья", "2025-06-01",
                                 article_topic="Другое", indexing="РИНЦ", db_path=target)
    assert r2["event_id"] == r1["event_id"] and r2["indexing_conflict"] == "ВАК", r2
    try:
        db.add_participation_ex(a, "X", "статья", "2025-06-01", indexing="Scopus", db_path=target)
        raise AssertionError("expected ValueError on bad indexing")
    except ValueError:
        pass
    try:
        db.add_participation_ex(a, "Y", "грант", "2025-06-01",
                                achievement_number="x" * 65, db_path=target)
        raise AssertionError("expected ValueError on long achievement number")
    except ValueError:
        pass
    # article fields ignored for non-article types
    r3 = db.add_participation_ex(a, "Стипендия Правительства", "стипендия", "2025-09-01",
                                 article_topic="ignored", indexing="ВАК", db_path=target)
    db.add_participation_ex(b, "Вторая статья", "статья", "2025-10-01", db_path=target)

    rows = {r["participation_id"]: r for r in db.list_participations_for_user(a, db_path=target)}
    art = rows[r1["participation_id"]]
    assert art["achievement_number"] == "ACH-001"
    assert art["article_topic"] == "ИИ" and art["indexing"] == "ВАК"
    sch = rows[r3["participation_id"]]
    assert sch["article_topic"] is None and sch["indexing"] is None

    # achievement number: set / change / clear / other user's row not touched
    assert db.set_achievement_number(r2["participation_id"], " B-7 ", user_id=b, db_path=target)
    assert not db.set_achievement_number(r2["participation_id"], "HACK", user_id=a, db_path=target)
    rb = db.list_participations_for_user(b, db_path=target)
    assert {r["achievement_number"] for r in rb} == {"B-7", None}
    assert db.set_achievement_number(r1["participation_id"], "", db_path=target)
    cleared = {r["participation_id"]: r for r in db.list_participations_for_user(a, db_path=target)}
    assert cleared[r1["participation_id"]]["achievement_number"] is None
    db.set_achievement_number(r1["participation_id"], "ACH-002", user_id=a, db_path=target)

    df, dt = db.year_range(2025)
    no_ach = db.list_all_participations(date_from=df, date_to=dt, without_achievement=True,
                                        db_path=target)
    assert r1["participation_id"] not in {r["participation_id"] for r in no_ach}
    assert r3["participation_id"] in {r["participation_id"] for r in no_ach}

    per = {r["login"]: r for r in db.stats_by_person(2025, db_path=target)}
    assert per["author1"]["articles"] == 1 and per["author1"]["scholarships"] == 1
    assert per["author2"]["articles"] == 2
    by_idx = {r["indexing"]: r for r in db.stats_articles_by_indexing(2025, db_path=target)}
    assert by_idx["ВАК"] == {"indexing": "ВАК", "articles": 1, "authors": 2}, by_idx
    assert by_idx["не указана"]["articles"] == 1
    summ = db.user_year_summary(b, 2025, db_path=target)
    assert summ["статья"] == 2 and summ["total"] == 2
    line = member_year_summary(2025, summ)
    assert "2 участия: 2 статьи" in line, line
    assert "1 стипендия, 5 статей" in member_year_summary(
        2025, {"total": 6, "стипендия": 1, "статья": 5})
    assert "3 стипендии" in member_year_summary(2025, {"total": 3, "стипендия": 3})

    import io

    import openpyxl

    import report
    parts = db.list_all_participations(date_from=df, date_to=dt, db_path=target)
    ws = openpyxl.load_workbook(io.BytesIO(report.build_year_xlsx(parts)))["Участия"]
    header = [c.value for c in ws[1]]
    assert "Номер достижения" in header and "Индексация" in header, header
    col = header.index("Номер достижения") + 1
    assert "ACH-002" in [ws.cell(row=i, column=col).value for i in range(2, ws.max_row + 1)]
    print("  new types / articles / achievement numbers OK")


def run_activity_kinds(target) -> None:  # noqa: ANN001
    uid = db.create_user("speaker", "pass12345", "Докладов Д.Д.", db_path=target)
    _, conf_eid = db.add_participation(uid, "ИТНО 2025", "конференция", "2025-09-10", db_path=target)
    _, other_eid = db.add_participation(uid, "Золотая Нива", "конкурс", "2025-06-16", db_path=target)
    offered = {e["event_id"]: e for e in db.unlinked_member_events(2025, db_path=target)}
    assert conf_eid in offered and other_eid in offered
    e = offered[conf_eid]
    assert e["suggested_kind"] == "Конференция"
    assert e["suggested_topic"] == "Выступление членов СНО в рамках «ИТНО 2025»"
    assert e["suggested_formats"] == ["Выступления с докладами"]
    assert offered[other_eid]["suggested_kind"] == "Другое"
    assert conf_eid not in {x["event_id"] for x in db.unlinked_member_events(2024, db_path=target)}

    before = db.count_meetings(2025, db_path=target)
    kid = db.add_meeting("2025-09-10", "13:30", "пос. Дивноморское, СОСК «Радуга»",
                         e["suggested_topic"], "Международная конференция, Выступления с докладами",
                         kind="Конференция", event_id=conf_eid, db_path=target)
    assert conf_eid not in {x["event_id"] for x in db.unlinked_member_events(2025, db_path=target)}
    try:
        db.add_meeting("2025-09-11", "13:30", "x", "x", "x", kind="Пикник", db_path=target)
        raise AssertionError("expected ValueError on bad kind")
    except ValueError:
        pass
    ms = db.list_meetings(2025, db_path=target)
    assert [m["number"] for m in ms] == list(range(1, len(ms) + 1))
    dates = [m["meeting_date"] for m in ms]
    assert dates == sorted(dates)  # one numbering across kinds, by date
    assert db.count_meetings(2025, db_path=target) == before + 1
    assert db.count_meetings(2025, kind="Заседание", db_path=target) == before
    assert db.count_meetings(2025, exclude_kind="Заседание", db_path=target) == 1
    assert db.get_meeting(kid, db_path=target)["kind"] == "Конференция"
    db.update_meeting(kid, "2025-09-10", "10:00", "Дивноморское", "Тема", "Форум", kind="Форум",
                      db_path=target)
    assert db.get_meeting(kid, db_path=target)["kind"] == "Форум"
    # deleting the event keeps the report row, link becomes NULL (ON DELETE SET NULL)
    db.delete_user(uid, db_path=target)
    with db.get_engine(target).begin() as conn:
        conn.execute(text("DELETE FROM events WHERE id = :e"), {"e": conf_eid})
    got = db.get_meeting(kid, db_path=target)
    assert got is not None and got["event_id"] is None, got
    print("  activity kinds / link to member events OK")


LEGACY_DDL = [  # schema of release 772cd4f (with CHECK on events.type)
    """CREATE TABLE users (id {pk}, login TEXT NOT NULL UNIQUE, password_hash TEXT NOT NULL,
        full_name TEXT NOT NULL, role TEXT NOT NULL CHECK(role IN ('admin', 'member')),
        created_at TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1)""",
    """CREATE TABLE events (id {pk}, title TEXT NOT NULL, title_norm TEXT NOT NULL,
        type TEXT NOT NULL CHECK(type IN ('грант', 'конференция', 'конкурс')),
        event_date TEXT NOT NULL, created_at TEXT NOT NULL, UNIQUE(title_norm, type, event_date))""",
    """CREATE TABLE participations (id {pk},
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        event_id INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
        created_at TEXT NOT NULL, UNIQUE(user_id, event_id))""",
    "CREATE INDEX idx_participations_event ON participations(event_id)",
]


def run_legacy_migration(target) -> None:  # noqa: ANN001
    """DB created by the previous release → init_db migrates it without data loss."""
    eng = db.get_engine(target)
    pk = ("INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY" if eng.dialect.name == "postgresql"
          else "INTEGER PRIMARY KEY AUTOINCREMENT")
    with eng.begin() as conn:
        for stmt in LEGACY_DDL:
            conn.execute(text(stmt.format(pk=pk)))
        conn.execute(text("INSERT INTO users (login, password_hash, full_name, role, created_at) "
                          "VALUES ('old', 'x', 'Старый У.У.', 'member', '2025-01-01')"))
        conn.execute(text("INSERT INTO events (title, title_norm, type, event_date, created_at) "
                          "VALUES ('Старый грант', 'старый грант', 'грант', '2025-02-02', 'x')"))
        conn.execute(text("INSERT INTO participations (user_id, event_id, created_at) "
                          "SELECT u.id, e.id, 'x' FROM users u, events e"))
        try:
            with conn.begin_nested():
                conn.execute(text("INSERT INTO events (title, title_norm, type, event_date, created_at) "
                                  "VALUES ('s', 's', 'статья', '2025-01-01', 'x')"))
            raise AssertionError("legacy CHECK should reject new type")
        except db.DuplicateError:
            pass
    db.init_db(db_path=target, seed_admin=False)
    db.init_db(db_path=target, seed_admin=False)  # idempotent
    old = db.get_user_by_login("old", db_path=target)
    rows = db.list_participations_for_user(old["id"], db_path=target)
    assert len(rows) == 1 and rows[0]["title"] == "Старый грант"
    assert rows[0]["achievement_number"] is None and rows[0]["indexing"] is None
    db.add_participation_ex(old["id"], "Новая статья", "статья", "2025-03-03",
                            indexing="РИНЦ", achievement_number="42", db_path=target)
    db.add_participation(old["id"], "Стипендия", "стипендия", "2025-03-04", db_path=target)
    db.add_meeting("2025-03-05", "13:30", "Ауд. 1", "Тема", "Собрание", db_path=target)
    db.delete_user(old["id"], db_path=target)  # FK cascade still works after rebuild
    assert db.list_all_participations(db_path=target) == []
    print("  legacy schema migration OK (CHECK removed, columns added, data kept)")


def run_schema_upgrade(target) -> None:  # noqa: ANN001
    """Old DB without meetings/settings → init_db adds them, existing rows untouched."""
    with db.get_engine(target).connect() as conn:
        before = {t: conn.execute(text(f"SELECT COUNT(*) FROM {t}")).scalar_one()
                  for t in ("users", "events", "participations")}
    with db.get_engine(target).begin() as conn:
        conn.execute(text("DROP TABLE meetings"))
        conn.execute(text("DROP TABLE settings"))
    db.init_db(db_path=target, seed_admin=True)
    with db.get_engine(target).connect() as conn:
        after = {t: conn.execute(text(f"SELECT COUNT(*) FROM {t}")).scalar_one()
                 for t in ("users", "events", "participations")}
    assert before == after, (before, after)
    assert db.list_meetings(db_path=target) == []
    assert db.get_report_settings(db_path=target)["sno_name"] == db.DEFAULT_SNO_NAME
    db.init_db(db_path=target)  # idempotent
    print(f"  schema upgrade OK (rows preserved: {after})")


def run_auth_cookie(target) -> None:  # noqa: ANN001
    import time

    import auth

    saved_secret = os.environ.pop("COOKIE_SECRET", None)
    auth._SECRETS_CACHE.clear()
    key1 = auth.get_cookie_secret(target)
    auth._SECRETS_CACHE.clear()
    assert auth.get_cookie_secret(target) == key1, "stored key must be stable"
    assert len(key1) >= 32
    assert db.get_app_setting("cookie_secret", db_path=target) == key1.decode()

    uid = db.create_user("cookie_u", "cookiepass1", "Куки К.К.", db_path=target)
    user = db.get_user_by_id(uid, db_path=target)
    tok = auth.make_auth_token(user, db_path=target)
    assert "cookiepass1" not in tok and user["password_hash"] not in tok
    assert auth.user_from_token(tok, db_path=target)["id"] == uid
    # expiry: valid at +29 days, invalid after 30 days
    now = time.time()
    assert auth.user_from_token(tok, db_path=target, now=now + 29 * 86400) is not None
    assert auth.user_from_token(tok, db_path=target, now=now + 30 * 86400 + 5) is None
    old = auth.make_auth_token(user, db_path=target, now=now - 31 * 86400)
    assert auth.user_from_token(old, db_path=target) is None
    # tamper: other user id / later expiry / garbage
    u_, e_, f_, s_ = tok.split(".")
    admin_id = db.get_user_by_login(os.environ.get("ADMIN_LOGIN") or "admin", db_path=target)["id"]
    for bad in (
        f"{admin_id}.{e_}.{f_}.{s_}", f"{u_}.{int(e_) + 999999}.{f_}.{s_}",
        f"{u_}.{e_}.{f_}.{'0' * len(s_)}", tok[:-1] + ("0" if tok[-1] != "0" else "1"),
        "", "abc", "1.2.3", None, "x.y.z.w",
    ):
        assert auth.user_from_token(bad, db_path=target) is None, bad
    # token signed with another key is rejected
    os.environ["COOKIE_SECRET"] = "some-other-secret-value-for-test-1234567890"
    try:
        assert auth.user_from_token(tok, db_path=target) is None
        tok_env = auth.make_auth_token(user, db_path=target)
        assert auth.user_from_token(tok_env, db_path=target)["id"] == uid
    finally:
        os.environ.pop("COOKIE_SECRET")
    assert auth.user_from_token(tok_env, db_path=target) is None
    # own password change invalidates old tokens; a fresh one works
    ok, _ = change_password(uid, "cookiepass1", "cookiepass2", "cookiepass2", db_path=target)
    assert ok
    assert auth.user_from_token(tok, db_path=target) is None
    fresh = auth.make_auth_token(db.get_user_by_id(uid, db_path=target), db_path=target)
    assert auth.user_from_token(fresh, db_path=target)["id"] == uid
    # admin resets password → invalid; disabled → invalid; deleted → invalid
    db.update_user(uid, password="cookiepass3", db_path=target)
    assert auth.user_from_token(fresh, db_path=target) is None
    fresh = auth.make_auth_token(db.get_user_by_id(uid, db_path=target), db_path=target)
    db.update_user(uid, full_name="Куки2", login="cookie_u2", db_path=target)
    assert auth.user_from_token(fresh, db_path=target) is not None, "rename keeps cookie"
    db.update_user(uid, active=False, db_path=target)
    assert auth.user_from_token(fresh, db_path=target) is None
    db.update_user(uid, active=True, db_path=target)
    assert auth.user_from_token(fresh, db_path=target) is not None
    db.delete_user(uid, db_path=target)
    assert auth.user_from_token(fresh, db_path=target) is None
    if saved_secret is not None:
        os.environ["COOKIE_SECRET"] = saved_secret
    auth._SECRETS_CACHE.clear()
    print("  auth cookie tokens OK (expiry, tamper, key, password change, disable, delete)")


def run_members_import(target) -> None:  # noqa: ANN001
    import io

    import openpyxl

    def xlsx(rows):  # noqa: ANN001, ANN202
        wb = openpyxl.Workbook()
        for r in rows:
            wb.active.append(r)
        buf = io.BytesIO()
        wb.save(buf)
        return buf.getvalue()

    db.create_user("imp_exist", "existpass1", "Уже Есть", db_path=target)
    old_hash = db.get_user_by_login("imp_exist", db_path=target)["password_hash"]
    data = xlsx([
        ["  логин ", "ПАРОЛЬ", "фио", "лишний"],  # any order / case / spaces
        ["imp_a", "passA1234", "Альфа А.А.", "x"],
        ["imp_b", 12345678, "Бета Б.Б.", None],  # numeric password
        ["imp_exist", "newpass999", "Чужой", None],  # existing → skip, untouched
        ["imp_a", "other", "Дубль", None],  # duplicate in file
        ["", "p", "Без логина", None],
        ["imp_c", None, "Без пароля", None],
        [None, None, None, None],  # blank row ignored
        ["bad login", "pass12345", "Пробел", None],
        ["кириллица", "pass12345", "Кир", None],
    ])
    rows = db.parse_members_xlsx(data, db_path=target)
    st_ = {r["row"]: r["status"] for r in rows}
    assert st_ == {
        2: db.ST_CREATE, 3: db.ST_CREATE, 4: db.ST_EXISTS, 5: db.ST_DUP,
        6: db.ST_EMPTY, 7: db.ST_EMPTY, 9: db.ST_BAD_LOGIN, 10: db.ST_BAD_LOGIN,
    }, st_
    assert db.import_members(rows, db_path=target) == (2, 6)
    b = db.get_user_by_login("imp_b", db_path=target)
    assert b["role"] == "member" and b["active"] and b["full_name"] == "Бета Б.Б."
    assert b["password_hash"] != "12345678" and verify_password("12345678", b["password_hash"])
    a = db.get_user_by_login("imp_a", db_path=target)
    assert a["full_name"] == "Альфа А.А." and verify_password("passA1234", a["password_hash"])
    ex = db.get_user_by_login("imp_exist", db_path=target)
    assert ex["password_hash"] == old_hash and ex["full_name"] == "Уже Есть"
    rows2 = db.parse_members_xlsx(data, db_path=target)  # second import: nothing new
    assert not any(r["ok"] for r in rows2)
    assert db.import_members(rows2, db_path=target) == (0, 8)
    for bad in (b"not an xlsx", xlsx([["ФИО", "Логин"], ["a", "b"]])):
        try:
            db.parse_members_xlsx(bad, db_path=target)
            raise AssertionError("ValueError expected")
        except ValueError:
            pass
    for login in ("imp_a", "imp_b", "imp_exist"):
        db.delete_user(db.get_user_by_login(login, db_path=target)["id"], db_path=target)
    print("  members import OK (statuses, create, skip existing, second import, bad file)")


def main() -> None:
    admin_pw_env = os.environ.get("ADMIN_PASSWORD")
    pg_url = os.environ.get("SMOKE_DATABASE_URL")

    if pg_url:
        if not admin_pw_env:
            os.environ["ADMIN_PASSWORD"] = admin_pw_env = "smoke-admin-pass"
        _reset_pg(pg_url)
        # Postgres without ADMIN_PASSWORD must NOT create an admin
        saved = os.environ.pop("ADMIN_PASSWORD")
        db.init_db(db_path=pg_url, seed_admin=False)
        assert db.ensure_admin(pg_url) == "missing_password"
        assert not db.admin_exists(pg_url)
        os.environ["ADMIN_PASSWORD"] = saved
        run(pg_url, expect_admin_password=admin_pw_env)
        _reset_pg(pg_url)
        run_legacy_migration(pg_url)
        _reset_pg(pg_url)
        print("OK (Postgres)")
        return

    tmp = Path(tempfile.mkdtemp()) / "sno_smoke.db"
    # Without ADMIN_PASSWORD local SQLite falls back to admin123
    run(tmp, expect_admin_password=admin_pw_env or "admin123")
    run_legacy_migration(tmp.with_name("sno_legacy.db"))
    print("OK")
    print(f"  smoke db: {tmp}")


if __name__ == "__main__":
    main()
