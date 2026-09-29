"""Smoke tests for the annual report (catalog, achievements, counting, migration, .docx).
Run through smoke_test.py (SQLite by default, Postgres via SMOKE_DATABASE_URL)."""

from __future__ import annotations

import hashlib
import io
import json

from sqlalchemy import text

import achievements as ach
import annual_fixtures as fx
import annual_report
import auth
import db
import report

NEW_TABLES = ("achievement_people", "achievements", "kind_subpoints", "kinds", "indicator_rows", "indicators")
ROWS_PER_INDICATOR = [4, 5, 4, 4, 4, 4, 4, 5, 4, 2, 4, 4, 8, 4, 4, 1, 0, 0]


def _admin(target) -> dict:  # noqa: ANN001
    return next(u for u in db.list_users(db_path=target) if u["role"] == "admin")


def run_catalog(target) -> None:  # noqa: ANN001
    cat = ach.catalog(target)
    assert [i["label"] for i in cat] == [x[1] for x in ach.INDICATORS]
    assert [len(i["rows"]) for i in cat] == ROWS_PER_INDICATOR
    assert [r["label"] for r in cat[1]["rows"]][4] == "- статья в журнале из перечня Белый список"
    with db.get_engine(target).begin() as conn:
        assert ach.seed_catalog(conn) == 0  # idempotent
    db.init_db(db_path=target)
    with db.get_engine(target).connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM indicators")).scalar_one() == 18
        assert conn.execute(text("SELECT COUNT(*) FROM indicator_rows")).scalar_one() == sum(ROWS_PER_INDICATOR)
        assert conn.execute(text("SELECT COUNT(*) FROM kinds")).scalar_one() == len(ach.KINDS)
    # admin edits survive restarts (seed never overwrites)
    row = cat[13]["rows"][3]  # 14 / городской
    ach.update_row(row["id"], "- городской уровень (г. Ростов-на-Дону)", True, db_path=target)
    ach.move_indicator(cat[17]["id"], -1, db_path=target)
    db.init_db(db_path=target)
    cat2 = ach.catalog(target)
    r2 = next(r for r in cat2[13]["rows"] if r["id"] == row["id"])
    assert r2["label"].endswith("(г. Ростов-на-Дону)") and r2["hidden"] == 1
    assert cat2[16]["code"] == "I18" and cat2[17]["code"] == "I17"
    ach.move_indicator(cat[17]["id"], +1, db_path=target)
    ach.update_row(row["id"], row["label"], False, db_path=target)
    new_id = ach.add_indicator("19. Тестовый показатель", "level", db_path=target)
    rid = ach.add_row(new_id, "- международный уровень", db_path=target)
    assert any(k["indicator_id"] == new_id for k in ach.list_kinds(target))
    ach.delete_row(rid, db_path=target)
    ach.delete_indicator(new_id, db_path=target)
    assert len(ach.catalog(target)) == 18
    # never hard-delete with records
    admin = _admin(target)
    m = db.create_user("cat_m", "pass12345", "Каталогов К.К.", db_path=target)
    K = ach.kind_by_code("contest", target)
    aid = ach.save_achievement({"kind_id": K["id"], "owner_id": m, "row_id": cat[2]["rows"][0]["id"],
                                "title": "Конкурс X", "date_from": "2025-05-05", "result": "участие"},
                               admin, db_path=target)["id"]
    for fn, arg in ((ach.delete_row, cat[2]["rows"][0]["id"]), (ach.delete_indicator, cat[2]["id"])):
        try:
            fn(arg, db_path=target)
            raise AssertionError("delete with records must fail")
        except ValueError:
            pass
    ach.delete_achievement(aid, db_path=target)
    db.delete_user(m, db_path=target)
    print("  annual catalog OK (18 indicators verbatim, idempotent seed, admin edits kept, no delete with records)")


def run_numbers_and_validation(target) -> None:  # noqa: ANN001
    assert ach.normalize_number(" № P-H-1700-25 ") == "Р-Н-1700-25"
    assert ach.normalize_number("Nº р-х-349-25") == "Р-Х-349-25"
    assert ach.normalize_number("Р – О – 805 – 25") == "Р-О-805-25"
    assert ach.normalize_number("") is None
    assert ach.academic_year_range("2025-2026") == ("2025-09-01", "2026-08-31")
    for bad in ("2025", "2025-2027"):
        try:
            ach.academic_year_range(bad)
            raise AssertionError(bad)
        except ValueError:
            pass
    ok = [{"name": "Б", "share": 48}, {"name": "В", "share": 1}]
    assert ach.validate_shares(49, ok) == 98
    for owner, co, msg in ((None, [], "главного"), (0, [], "больше 0"), (101, [], "больше 0"),
                           (50, [{"name": "Б", "share": None}], "укажите долю"),
                           (50, [{"name": "", "share": 10}], "ФИО"),
                           (60, [{"name": "Б", "share": 50}], "больше 100")):
        try:
            ach.validate_shares(owner, co)
            raise AssertionError((owner, co))
        except ValueError as e:
            assert msg in str(e), (msg, str(e))
    print("  numbers normalized (Latin P-H → Р-Н, №/Nº stripped), shares validated")


def run_fixtures_report(target) -> None:  # noqa: ANN001
    res = fx.load(target)
    uid = res["users"]
    data = ach.report_data(2025, db_path=target)
    counts = {r["label"] and (ind["code"], i): r["count"]
              for ind in data["indicators"] for i, r in enumerate(ind["rows"])}
    exp = {("I01", 0): 5, ("I01", 1): 0, ("I01", 2): 1, ("I02", 0): 1, ("I02", 2): 1, ("I02", 3): 1,
           ("I02", 4): 1, ("I03", 1): 3, ("I03", 3): 2, ("I04", 0): 1, ("I04", 1): 2, ("I07", 1): 1,
           ("I08", 0): 1, ("I08", 1): 3, ("I08", 2): 1, ("I08", 4): 1, ("I09", 0): 2, ("I09", 1): 1,
           ("I09", 3): 1, ("I10", 0): 2, ("I11", 0): 2, ("I12", 2): 1, ("I15", 1): 2, ("I16", 0): 1}
    for k, v in counts.items():
        assert v == exp.get(k, 0), (k, v, exp.get(k, 0))
    totals = {i["code"]: i["count"] for i in data["indicators"]}
    assert totals["I01"] == 6 and totals["I02"] == 4 and totals["I17"] == 1 and totals["I18"] == 1
    assert totals["I05"] == 0 and totals["I13"] == 0
    assert data["total"] == 39 and data["zaochno"] == 1 and data["warnings"] == []
    # several dokladov by one person at one event: one event line, two numbered lines
    items = next(i for i in data["indicators"] if i["code"] == "I01")["rows"][0]["items"]
    ev = [x for x in items if x[0] == "event"]
    assert len(ev) == 2 and sum(1 for x in items if x[0] == "name" and x[1] == fx.PEOPLE["starostin"]) == 3
    forum_i = next(n for n, x in enumerate(items) if x[0] == "event" and x[1].startswith("Международный"))
    forum_lines = [x for x in items[forum_i + 1:] if x[0] == "name"]
    assert len(forum_lines) == 3 and all("доклад на тему: «" in x[2] for x in forum_lines)
    assert any(x[2].endswith("Р-Н-5638-25") for x in forum_lines)  # Latin P-H normalized
    # publication line: shares in author order, co-authors printed, counted once
    pub = next(i for i in data["indicators"] if i["code"] == "I02")
    vak = pub["rows"][2]["items"][0][1]
    assert vak.startswith("(49%) Саркисян Д.С., (48%) Чолутаева Э.Э., (1%) Шевченко В.Н. Sarkisyan, D. S.")
    assert "Р-Н-6100-25" in vak and "ссылка: https://elibrary.ru/item.asp?id=80000001" in vak
    rinc = pub["rows"][3]["items"][0][1]
    assert rinc.startswith("Мартынюк И.О., Старостин Д.В. Методы") and "DOI: 10.23947/interagro.2025.117-119." in rinc
    assert "Доля участия: 50%, 50%." in rinc
    scopus = pub["rows"][0]["items"][0][1]
    assert scopus.startswith("(10%) Старостин Д.В., (10%) Марченко С.А., (10%) Мартынюк И.О. Starostin, D.")
    assert "(2025). Исследование наличия" in scopus
    # per-person: publication counts only for the main author
    per = {r["login"]: r for r in ach.stats_by_person(2025, db_path=target)}
    assert per["martynuk"]["by_kind"].get("Публикация") == 1
    assert "Публикация" not in per["katanaeva"]["by_kind"]
    # contest result printed at the end of the line; «участие» is not printed
    univ = next(i for i in data["indicators"] if i["code"] == "I03")["rows"][3]["items"]
    lines3 = [x[1] for x in univ]
    assert any(x.endswith("Р-П-1444-25, диплом II степени") for x in lines3), lines3
    assert not any("участие" in x for x in lines3), lines3
    # publication «Без индексации»: valid record, not printed, no warning
    admin_u = next(u for u in db.list_users(db_path=target) if u["role"] == "admin")
    pub_k = next(k for k in ach.list_kinds(target, admin=True) if k["form"] == "publication")
    nid = ach.save_achievement({"kind_id": pub_k["id"], "owner_id": uid["gaidai"], "row_id": ach.NO_INDEX,
                                "title": "Статья в сборнике без индексации", "journal": "Сборник",
                                "year": 2025, "owner_share": 100.0}, admin_u, db_path=target)["id"]
    rec = ach.get_achievement(nid, db_path=target)
    assert rec["details"].get("no_index") and rec["row_id"] is None and not rec["counted"] and rec["issues"] == []
    data2 = ach.report_data(2025, db_path=target)
    assert data2["total"] == 39 and data2["warnings"] == []
    ach.delete_achievement(nid, db_path=target)
    assert per["starostin"]["by_kind"]["Публикация"] == 1  # own Scopus article only
    # year filter: stipend 2024-2025 in 2024 and 2025; 2025-2026 in 2025 and 2026
    s24 = [r for r in ach.list_achievements(year=2024, db_path=target) if r["form"] == "stipend"]
    s26 = [r for r in ach.list_achievements(year=2026, db_path=target) if r["form"] == "stipend"]
    assert len(s24) == 2 and len(s26) == 4
    # duplicate publication (title+year / DOI) → refused with the owner's name
    K = ach.kind_by_code("publication", target)
    member = db.get_user_by_id(uid["katanaeva"], db_path=target)
    base = {"kind_id": K["id"], "row_id": ach.row_by_code("I02.rinc", target)["id"], "owner_share": 100,
            "journal": "Ж", "year": 2025}
    for dup in ({"title": "  методы внесения ПРОБИОТИКОВ в комбикорма: обзор "},
                {"title": "Другое название", "doi": "https://doi.org/10.23947/INTERAGRO.2025.117-119"}):
        try:
            ach.save_achievement({**base, **dup}, member, db_path=target)
            raise AssertionError("duplicate must be refused")
        except ach.DuplicateAchievement as e:
            assert fx.PEOPLE["martynuk"] in str(e)
    try:
        ach.save_achievement({**base, "title": "Новая", "owner_share": 70,
                              "coauthors": [{"name": "Внешний А.А.", "share": 40}]}, member, db_path=target)
        raise AssertionError("total > 100 must be refused")
    except ValueError as e:
        assert "больше 100" in str(e)
    # member rights: no admin kinds, only own records
    try:
        ach.save_achievement({"kind_id": ach.kind_by_code("agreement", target)["id"], "title": "x",
                              "agreement": "1", "date_from": "2025-01-01"}, member, db_path=target)
        raise AssertionError("member must not add admin kinds")
    except ValueError:
        pass
    other = ach.list_achievements(owner_id=uid["martynuk"], db_path=target)[0]
    try:
        ach.save_achievement({"kind_id": other["kind_id"], "title": "x"}, member, achievement_id=other["id"],
                             db_path=target)
        raise AssertionError("member must not edit others")
    except ValueError:
        pass
    assert not ach.delete_achievement(other["id"], owner_id=member["id"], db_path=target)
    # member stats see no names
    ov = ach.events_overview(2025, db_path=target)
    assert ov and all(set(e) == {"title", "kind", "date", "records", "people"} for e in ov)
    blob = json.dumps(ov, ensure_ascii=False)
    assert not any(n in blob for n in fx.PEOPLE.values())

    # .docx: landscape, title, every indicator/sub-row verbatim with counts, conclusion, signatures
    b = annual_report.build_annual_docx(data, ach.get_conclusion(2025, db_path=target),
                                        db.get_report_settings(db_path=target)["signatories"])
    check_docx(b, data)  # conclusion/signatures from fixtures
    x = annual_report.build_achievements_xlsx(data, db.list_meetings(2025, db_path=target))
    from openpyxl import load_workbook

    wb = load_workbook(io.BytesIO(x))
    assert wb.sheetnames == ["Достижения", "Показатели", "Заседания"]
    assert wb["Достижения"].max_row == len(data["records"]) + 1
    # the existing meetings report («Приложение Е») still generates alongside
    mid = db.add_meeting("2025-03-01", "13:30", "Ауд. 1", "Заседание СНО", "Собрание", db_path=target)
    mb = report.build_meetings_docx(db.list_meetings(2025, db_path=target), 2025, fx.SNO_NAME,
                                    db.DEFAULT_APPENDIX_LABEL, fx.SIGNATORIES)
    from docx import Document

    mdoc = Document(io.BytesIO(mb))
    mtext = "\n".join(p.text for p in mdoc.paragraphs)
    assert "Приложение Е" in mtext and "Сельское хозяйство" in mtext
    assert any("Заседание СНО" in c.text for t in mdoc.tables for row in t.rows for c in row.cells)
    db.delete_meeting(mid, db_path=target)
    print(f"  annual report OK: {data['total']} units, docx {len(b)} bytes, xlsx {len(x)} bytes; "
          "meetings report still OK")


def check_docx(b: bytes, data: dict) -> None:
    from docx import Document
    from docx.enum.section import WD_ORIENT
    from docx.oxml.ns import qn

    d = Document(io.BytesIO(b))
    sec = d.sections[0]
    assert sec.orientation == WD_ORIENT.LANDSCAPE and sec.page_width > sec.page_height
    paras = [p.text for p in d.paragraphs]
    assert paras[0] == f"Отчет о работе СНО «{data['sno_name']}»" and paras[1] == f"За {data['year']} год"
    t0, t1 = d.tables
    assert [c.text for c in t0.rows[0].cells] == ["Показатель", "Количество",
                                                  "Отметка о выполнении с указанием детальной информации"]
    rows = [r for t in (t0, t1) for r in t.rows][1:]
    expected = []
    for ind in data["indicators"]:
        expected.append((ind["label"], str(ind["count"]), True))
        expected += [(r["label"], str(r["count"]) if r["count"] else "", False) for r in ind["rows"]]
    assert len(rows) == len(expected), (len(rows), len(expected))
    for r, (label, cnt, bold) in zip(rows, expected):
        cells = r.cells
        assert cells[0].text == label, (cells[0].text, label)
        assert cells[1].text == cnt, (label, cells[1].text, cnt)
        assert bool(cells[0].paragraphs[0].runs[0].bold) == bold
    assert len(t0.rows) == 1 + sum(1 + len(i["rows"]) for i in data["indicators"][:5])
    # numbered detail lines restart per sub-row (separate w:num per cell)
    nums = []
    for r in rows:
        ids = {p._p.find(qn("w:pPr") + "/" + qn("w:numPr") + "/" + qn("w:numId")).get(qn("w:val"))
               for p in r.cells[2].paragraphs
               if p._p.find(qn("w:pPr") + "/" + qn("w:numPr")) is not None}
        assert len(ids) <= 1
        nums += list(ids)
    assert len(nums) == len(set(nums)) and nums
    body = "\n".join(paras)
    assert fx.CONCLUSION in body
    for sig in fx.SIGNATORIES:
        assert f"{sig['position']} {annual_report.SIGNATURE_LINE}/ {sig['name']}" in body
    assert [c.width for c in t0.rows[0].cells] == [c.width for c in t1.rows[0].cells]  # template grid


def run_orphans_and_meetings(target) -> None:  # noqa: ANN001
    admin = _admin(target)
    u = db.create_user("orph_a", "pass12345", "Сиротин С.С.", db_path=target)
    K = ach.kind_by_code("doklad", target)
    r = ach.row_by_code("I01.reg", target)["id"]
    a1 = ach.save_achievement({"kind_id": K["id"], "owner_id": u, "row_id": r, "title": "Форум Сирот",
                               "date_from": "2025-09-09", "topic": "Т1", "ochno": True}, admin, db_path=target)["id"]
    a2 = ach.save_achievement({"kind_id": K["id"], "owner_id": u, "row_id": r, "title": "форум сирот ",
                               "date_from": "2025-09-09", "topic": "Т2", "ochno": True}, admin, db_path=target)["id"]
    e1 = ach.get_achievement(a1, target)["event_id"]
    assert e1 and e1 == ach.get_achievement(a2, target)["event_id"]  # same event, 2 dokladov
    offered = {e["event_id"]: e for e in db.unlinked_member_events(2025, db_path=target)}
    assert offered[e1]["participants_count"] == 1 and offered[e1]["suggested_kind"] == "Конференция"
    mid = db.add_meeting("2025-09-09", "13:30", "Ауд", "Выступления", "Конференция", kind="Конференция",
                         event_id=e1, db_path=target)
    assert ach.delete_achievement(a1, db_path=target)
    assert db.get_meeting(mid, db_path=target)["event_id"] == e1  # still used by a2
    assert ach.delete_achievement(a2, db_path=target)
    with db.get_engine(target).connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM events WHERE id = :e"), {"e": e1}).scalar_one() == 0
    m = db.get_meeting(mid, db_path=target)
    assert m is not None and m["event_id"] is None  # meeting row kept in the report
    db.delete_meeting(mid, db_path=target)
    db.delete_user(u, db_path=target)
    print("  achievements: event grouping, orphan cleanup, meeting row kept OK")


def _md5(target, tables=("users", "events", "participations", "meetings", "settings")) -> dict:  # noqa: ANN001
    out = {}
    with db.get_engine(target).connect() as conn:
        for t in tables:
            rows = conn.execute(text(f"SELECT * FROM {t} ORDER BY 1")).all()
            out[t] = hashlib.md5(repr([tuple(str(v) for v in r) for r in rows]).encode()).hexdigest()
    return out


def run_migration(target) -> None:  # noqa: ANN001
    """DB of the current release (no achievement tables) with data → init_db migrates it."""
    import auth

    db.init_db(db_path=target, seed_admin=True)
    with db.get_engine(target).begin() as conn:
        for t in NEW_TABLES:
            conn.execute(text(f"DROP TABLE {t}"))
    a = db.create_user("mig_a", "pass12345", "Первый Автор", db_path=target)
    b = db.create_user("mig_b", "pass12345", "Второй Автор", db_path=target)
    c = db.create_user("mig_c", "pass12345", "Третий Член", db_path=target)
    add = lambda u, *a_, **k: db.add_participation_ex(u, *a_, db_path=target, **k)  # noqa: E731
    add(a, "Конференция ИТНО", "конференция", "2025-09-10", achievement_number="P-H-1-25")
    add(b, "Конференция ИТНО", "конференция", "2025-09-10", achievement_number="Р-Н-2-25")
    add(a, "Статья про корма", "статья", "2025-05-01", article_topic="корма", indexing="ВАК")
    add(b, "Статья про корма", "статья", "2025-05-01", article_topic="корма", indexing="ВАК",
        achievement_number="Р-Н-3-25")
    add(c, "Статья без индекса", "статья", "2025-06-01", indexing="Без индексации")
    add(b, "Кейс-чемпионат", "конкурс", "2025-04-04", achievement_number="Р-Н-4-25")
    add(c, "Стипендия Губернатора", "стипендия", "2025-02-01")
    add(a, "УМНИК 2025", "грант", "2025-03-03")
    eid = db.get_or_create_event("Конференция ИТНО", "конференция", "2025-09-10", db_path=target)
    db.add_meeting("2025-09-10", "13:30", "Ауд", "ИТНО", "Конференция", kind="Конференция", event_id=eid,
                   db_path=target)
    db.save_report_settings("Сельское хозяйство", "Приложение Е", fx.SIGNATORIES, db_path=target)
    tok = auth.make_auth_token(db.get_user_by_id(a, db_path=target), db_path=target)
    before = _md5(target)
    with db.get_engine(target).connect() as conn:
        n_parts = conn.execute(text("SELECT COUNT(*) FROM participations")).scalar_one()

    db.init_db(db_path=target)  # migration
    db.init_db(db_path=target)  # idempotent
    assert _md5(target) == before, "legacy tables must stay byte-identical"
    recs = ach.list_achievements(db_path=target)
    with db.get_engine(target).connect() as conn:
        n_people = conn.execute(text("SELECT COUNT(*) FROM achievement_people")).scalar_one()
    assert len(recs) + n_people == n_parts == 8, (len(recs), n_people, n_parts)
    by = {(r["kind_code"], r["owner_login"], r["title"]): r for r in recs}
    d1 = by[("doklad", "mig_a", "Конференция ИТНО")]
    assert d1["number"] == "Р-Н-1-25" and d1["ochno"] == 1 and d1["row_id"] is None and d1["event_id"] == eid
    assert "укажите уровень" in d1["issues"] and not d1["counted"]
    art = by[("publication", "mig_a", "Статья про корма")]  # main author = first added
    assert art["row_code"] == "I02.vak" and [p["name"] for p in art["people"]] == ["Второй Автор"]
    assert art["details"]["coauthor_numbers"] == ["Р-Н-3-25"] and "заполните долю" in art["issues"]
    assert art["counted"]
    noidx = by[("publication", "mig_c", "Статья без индекса")]
    assert noidx["row_id"] is None and noidx["details"].get("no_index") and not noidx["counted"]
    assert not any("индексац" in i for i in noidx["issues"])
    assert by[("contest", "mig_b", "Кейс-чемпионат")]["number"] == "Р-Н-4-25"
    assert by[("stipend", "mig_c", "Стипендия Губернатора")]["row_code"] == "I08.other"
    g = by[("grant", "mig_a", "УМНИК 2025")]
    assert g["subpoint_id"] is None and "выберите подпункт гранта" in g["issues"]
    data = ach.report_data(2025, db_path=target)
    assert len(data["warnings"]) == 6 and data["total"] == 2  # article (ВАК) + stipend «иные»
    # users, roles, cookies, meetings keep working
    assert auth.user_from_token(tok, db_path=target)["id"] == a
    assert db.list_meetings(2025, db_path=target)[0]["event_id"] == eid
    assert db.get_user_by_login("mig_c", db_path=target)["role"] == "member"
    assert auth.verify_password("pass12345", db.get_user_by_login("mig_b", db_path=target)["password_hash"])
    # owner fills the shares → warning gone; deleting a migrated record removes its legacy rows
    admin = _admin(target)
    ach.save_achievement({"kind_id": art["kind_id"], "owner_id": a, "row_id": art["row_id"],
                          "title": art["title"], "journal": "Вестник", "year": 2025, "owner_share": 60,
                          "coauthors": [{"user_id": b, "name": "Второй Автор", "share": 40}]},
                         admin, achievement_id=art["id"], db_path=target)
    art2 = ach.get_achievement(art["id"], target)
    assert art2["issues"] == [] and ach.shares_text(art2) == "доля участия: 60%, 40%"
    db.init_db(db_path=target)
    assert len(ach.list_achievements(db_path=target)) == len(recs)  # nothing re-imported
    assert ach.delete_achievement(d1["id"], db_path=target)
    db.init_db(db_path=target)
    assert len(ach.list_achievements(db_path=target)) == len(recs) - 1
    assert db.list_meetings(2025, db_path=target)[0]["event_id"] == eid  # b's doklad still uses it
    print(f"  migration from current schema OK ({n_parts} participations → {len(recs)} records "
          f"+ {n_people} co-author; legacy md5 unchanged; idempotent; warnings={len(data['warnings'])})")


def run_cache(target) -> None:  # noqa: ANN001
    """Read cache: hits skip SQL, any write (any code path) invalidates, copies are isolated."""
    from sqlalchemy import event
    eng = db.get_engine(target)
    n = {"q": 0}

    def _count(*_a, **_k):  # noqa: ANN002, ANN003
        n["q"] += 1
    event.listen(eng, "before_cursor_execute", _count)
    try:
        users = db.list_users(db_path=target)
        n["q"] = 0
        again = db.list_users(db_path=target)
        assert n["q"] == 0 and again == users  # served from cache
        again[0]["full_name"] = "ИЗМЕНЕНО"  # caller mutation must not leak into the cache
        assert db.list_users(db_path=target)[0]["full_name"] != "ИЗМЕНЕНО"
        uid = db.create_user("cache_probe", "pass12345", "Кэш Проба", "member", db_path=target)
        assert any(u["id"] == uid for u in db.list_users(db_path=target))  # visible immediately
        db.set_app_setting("sno_name", "Кэш-тест", db_path=target)
        assert db.get_report_settings(db_path=target)["sno_name"] == "Кэш-тест"
        k = ach.list_kinds(target, admin=True)[0]
        ach.rename_kind(k["id"], k["label"] + " *", bool(k["hidden"]), db_path=target)
        assert ach.list_kinds(target, admin=True)[0]["label"] == k["label"] + " *"
        ach.rename_kind(k["id"], k["label"], bool(k["hidden"]), db_path=target)
        db.set_app_setting("sno_name", "Сельское хозяйство", db_path=target)
        db.delete_user(uid, db_path=target)
        assert not any(u["id"] == uid for u in db.list_users(db_path=target))
        ach.catalog(target)
        n["q"] = 0
        ach.catalog(target)
        assert n["q"] == 0  # cached until the next write
    finally:
        event.remove(eng, "before_cursor_execute", _count)
    print("  read cache: hits, isolation, invalidation on writes OK")


def run_coauthors(target) -> None:  # noqa: ANN001
    """Linked co-authors see the article read-only; report / SNO stats count it once."""
    db.init_db(db_path=target, seed_admin=True)
    admin = _admin(target)
    a = db.create_user("co_a", "pass12345", "Авторов Андрей Андреевич", db_path=target)
    b = db.create_user("co_b", "pass12345", "Бетова Белла Борисовна", db_path=target)
    c = db.create_user("co_c", "pass12345", "Цветков Цезарь Цезаревич", db_path=target)
    A, B, C = (db.get_user_by_id(x, db_path=target) for x in (a, b, c))
    K = ach.kind_by_code("publication", target)
    rinc = ach.row_by_code("I02.rinc", target)["id"]
    base_total = ach.report_data(2026, db_path=target)["total"]
    art = {"kind_id": K["id"], "row_id": rinc, "title": "Совместная статья о кормах", "journal": "Вестник",
           "year": 2026, "doi": "10.1000/co.1", "owner_share": 50,
           "coauthors": [{"user_id": b, "name": B["full_name"], "share": 30},
                         {"name": "Внешний Виктор Викторович", "share": 20}]}
    aid = ach.save_achievement(art, A, db_path=target)["id"]
    # free-text co-author with a member's exact name: no name matching, C sees nothing
    txt = ach.save_achievement({**art, "title": "Статья с текстовым соавтором", "doi": "",
                                "coauthors": [{"name": C["full_name"], "share": 50}]}, A, db_path=target)["id"]
    mine_b = ach.list_achievements(owner_id=b, with_coauthored=True, db_path=target)
    assert [r["id"] for r in mine_b] == [aid] and mine_b[0]["role"] == "coauthor"
    assert mine_b[0]["my_share"] == 30 and mine_b[0]["owner_name"] == A["full_name"]
    assert ach.list_achievements(owner_id=b, db_path=target) == []  # owner-only view unchanged
    assert ach.list_achievements(owner_id=c, with_coauthored=True, db_path=target) == []
    mine_a = ach.list_achievements(owner_id=a, with_coauthored=True, db_path=target)
    assert {r["id"] for r in mine_a} == {aid, txt} and all(r["role"] == "owner" for r in mine_a)
    assert ach.member_year_summary(b, 2026, db_path=target) == [("Публикация", 1)]
    assert ach.coauthored_count(b, 2026, db_path=target) == 1 and ach.coauthored_count(a, 2026, db_path=target) == 0
    assert ach.short_name(A["full_name"]) == "Авторов А.А."
    # read-only for the co-author, owner and admin may edit
    try:
        ach.save_achievement({**art, "title": "Взлом"}, B, achievement_id=aid, db_path=target)
        raise AssertionError("co-author must not edit")
    except ValueError as e:
        assert "только свои" in str(e)
    assert not ach.delete_achievement(aid, owner_id=b, db_path=target)
    # report and SNO-level stats: exactly once
    data = ach.report_data(2026, db_path=target)
    assert data["total"] == base_total + 2
    ids = [r["id"] for ind in data["indicators"] for row in ind["rows"] for r in row["records"]]
    assert ids.count(aid) == 1
    per = {p["user_id"]: p for p in ach.stats_by_person(2026, db_path=target)}
    assert per[a]["total"] == 2 and b not in per  # Топ-5: the owner only
    recs = ach.list_achievements(year=2026, db_path=target)
    assert [r["id"] for r in recs].count(aid) == 1
    ov = [e for e in ach.events_overview(2026, db_path=target) if e["title"] == art["title"]]
    assert len(ov) == 1 and ov[0]["records"] == 1 and ov[0]["people"] == 2
    # second author tries to enter the same article → hint, nothing saved
    for who, needle in ((B, "уже указаны в ней соавтором"), (C, "попросите"), (A, "ваша запись")):
        try:
            ach.save_achievement({**art, "coauthors": [], "owner_share": 100}, who, db_path=target)
            raise AssertionError("duplicate must be refused")
        except ach.DuplicateAchievement as e:
            assert needle in str(e), str(e)
    # validation: owner is not his own co-author, a member only once
    for bad in ([{"user_id": a, "name": "x", "share": 10}],
                [{"user_id": b, "name": "x", "share": 10}, {"user_id": b, "name": "x", "share": 10}]):
        try:
            ach.save_achievement({**art, "title": "Новая", "doi": "", "coauthors": bad}, A, db_path=target)
            raise AssertionError("bad co-authors must be refused")
        except ValueError:
            pass
    # safety net: the same article stored twice (e.g. legacy / direct DB) counts once
    with db.get_engine(target).begin() as conn:
        conn.execute(text(
            "INSERT INTO achievements (kind_id, subpoint_id, indicator_id, row_id, owner_id, title, date_from, "
            "date_to, ochno, owner_share, details, created_at) SELECT kind_id, subpoint_id, indicator_id, row_id, "
            ":b, title, date_from, date_to, ochno, 100, details, created_at FROM achievements WHERE id = :id"),
            {"b": b, "id": aid})
    data2 = ach.report_data(2026, db_path=target)
    assert data2["total"] == data["total"]
    dup = next(r for r in ach.list_achievements(owner_id=b, db_path=target))
    assert not dup["counted"] and dup["dup_of"]["id"] == aid and any("дубль" in i for i in dup["issues"])
    assert any(w["id"] == dup["id"] for w in data2["warnings"])
    assert ach.delete_achievement(dup["id"], owner_id=b, db_path=target)
    # cache invalidation: owner removes the co-author → gone from B's list immediately
    ach.save_achievement({**art, "coauthors": [{"name": "Внешний Виктор Викторович", "share": 20}]}, A,
                         achievement_id=aid, db_path=target)
    assert ach.list_achievements(owner_id=b, with_coauthored=True, db_path=target) == []
    assert ach.member_year_summary(b, 2026, db_path=target) == []
    print("  co-authors: linked member sees article read-only, free text ignored, report/stats count once, "
          "duplicate hint + safety net OK")


def _pub(target, owner, extra=None, **kw):  # noqa: ANN001, ANN003
    K = ach.kind_by_code("publication", target)
    base = {"kind_id": K["id"], "owner_id": owner["id"], "journal": "Тестовый журнал", "owner_share": 100}
    return ach.save_achievement({**base, **(extra or {}), **kw}, _admin(target), db_path=target)["id"]


def run_v4_pubdate(target) -> None:  # noqa: ANN001
    """Item 1: full publication date; year-only records stay valid; dedupe unchanged."""
    db.init_db(db_path=target, seed_admin=True)
    u = db.get_user_by_id(db.create_user("pd_a", "pass12345", "Датов Дмитрий Дмитриевич", db_path=target),
                          db_path=target)
    vak = ach.row_by_code("I02.vak", target)["id"]
    # new record with a full date
    a = _pub(target, u, row_id=vak, title="Статья с датой", pub_date="2027-03-15", year=None)
    rec = ach.get_achievement(a, target)
    assert rec["date_from"] == "2027-03-15" and rec["date_to"] is None
    assert rec["details"]["pub_date"] == "2027-03-15" and rec["details"]["year"] == 2027
    assert ach.pub_date_text(rec) == "15.03.2027"
    assert rec["id"] in [r["id"] for r in ach.list_achievements(year=2027, db_path=target)]
    assert a not in [r["id"] for r in ach.list_achievements(year=2026, db_path=target)]
    line = ach.record_line(rec)
    assert "(2027). Статья с датой." in line and "15.03.2027" not in line
    # old shape: only «year» (API callers, legacy rows) -> 01.01-31.12, no pub_date, still valid
    b = _pub(target, u, row_id=vak, title="Старая статья только с годом", year=2027)
    old = ach.get_achievement(b, target)
    assert (old["date_from"], old["date_to"]) == ("2027-01-01", "2027-12-31") and "pub_date" not in old["details"]
    assert ach.pub_date_text(old) == "2027" and old["issues"] == []
    # editing an old record with the untouched default date (01.01) keeps it year-only
    ach.save_achievement({"kind_id": old["kind_id"], "owner_id": u["id"], "row_id": vak, "title": old["title"],
                          "journal": "Тестовый журнал", "owner_share": 100, "year": 2027, "pub_date": "2027-01-01"},
                         u, achievement_id=b, db_path=target)
    old2 = ach.get_achievement(b, target)
    assert (old2["date_from"], old2["date_to"]) == ("2027-01-01", "2027-12-31") and "pub_date" not in old2["details"]
    # ... and a picked date turns it into a dated record
    ach.save_achievement({"kind_id": old["kind_id"], "owner_id": u["id"], "row_id": vak, "title": old["title"],
                          "journal": "Тестовый журнал", "owner_share": 100, "pub_date": "2027-11-05"},
                         u, achievement_id=b, db_path=target)
    assert ach.get_achievement(b, target)["date_from"] == "2027-11-05"
    # an update that passes no date keeps the stored one
    ach.save_achievement({"kind_id": old["kind_id"], "owner_id": u["id"], "row_id": vak, "title": old["title"],
                          "journal": "Другой журнал", "owner_share": 100, "year": 2027}, u, achievement_id=b,
                         db_path=target)
    assert ach.get_achievement(b, target)["date_from"] == "2027-11-05"
    # validation
    for bad in ({"pub_date": "2027-13-40"}, {"pub_date": "1800-01-01"}, {"pub_date": None, "year": None}):
        try:
            _pub(target, u, row_id=vak, title="Плохая дата", **bad)
            raise AssertionError(bad)
        except ValueError:
            pass
    # dedupe by title+year (any day of the year) and by DOI keeps working across shapes
    for dup in ({"title": " СТАТЬЯ с датой ", "pub_date": "2027-12-31"}, {"title": "Статья с датой", "year": 2027},
                {"title": "Иное", "pub_date": "2027-06-01", "doi": None}):
        if dup["title"] == "Иное":
            _pub(target, u, row_id=vak, title="Иное", pub_date="2027-06-01", doi="10.1/pd.1")
            dup = {"title": "Совсем другое", "pub_date": "2027-07-07", "doi": "https://doi.org/10.1/PD.1"}
        try:
            _pub(target, u, row_id=vak, **dup)
            raise AssertionError(dup)
        except ach.DuplicateAchievement:
            pass
    # different year -> not a duplicate
    c = _pub(target, u, row_id=vak, title="Статья с датой", pub_date="2028-03-15")
    assert c
    # safety net (reports): dated + year-only copy of the same article counts once
    with db.get_engine(target).begin() as conn:
        conn.execute(text("INSERT INTO achievements (kind_id, indicator_id, row_id, owner_id, title, date_from, "
                          "date_to, ochno, owner_share, details, created_at) SELECT kind_id, indicator_id, row_id, "
                          "owner_id, title, '2027-01-01', '2027-12-31', ochno, 100, "
                          "'{\"year\": 2027, \"journal\": \"Ж\"}', created_at FROM achievements WHERE id = :id"),
                     {"id": a})
    data = ach.report_data(2027, db_path=target)
    assert sum(1 for r in data["records"] if normalize(r["title"]) == "статья с датой" and r["counted"]) == 1
    # the date appears in the annual report line and in the Excel export
    vak_row = next(row for i in data["indicators"] if i["code"] == "I02" for row in i["rows"] if row["id"] == vak)
    assert any("(2027). " in it[1] and "15.03.2027" not in it[1] for it in vak_row["items"])
    assert b"" != annual_report.build_achievements_xlsx(data)
    for r in ach.list_achievements(owner_id=u["id"], db_path=target):
        ach.delete_achievement(r["id"], db_path=target)
    with db.get_engine(target).begin() as conn:
        conn.execute(text("DELETE FROM achievements WHERE owner_id = :o"), {"o": u["id"]})
    db.delete_user(u["id"], db_path=target)
    print("  publication date: full date stored/edited, year-only records stay valid, dedupe by DOI / title+year OK")


def normalize(t: str) -> str:
    return db.normalize_title(t)


def run_v4_bibformat(target) -> None:  # noqa: ANN001
    """Item 4: ВАК / Белый список (and Scopus/WoS, РИНЦ) print ФИО of owner + co-authors with shares
    and the bibliographic data in the sample format."""
    db.init_db(db_path=target, seed_admin=True)
    ids = {n: db.create_user(f"bf_{n}", "pass12345", full, db_path=target) for n, full in
           (("o", "Козырев Дмитрий Сергеевич"), ("c", "Поляков Александр"), ("d", "Одабашян Михаил Гагикович"))}
    owner = db.get_user_by_id(ids["o"], db_path=target)
    co = [{"user_id": ids["c"], "name": "Поляков Александр", "share": 10},
          {"user_id": ids["d"], "name": "Одабашян Михаил Гагикович", "share": 10},
          {"user_id": None, "name": "Иванова Анна Петровна", "share": 5}]
    common = dict(owner_share=70, coauthors=co, journal="Siberian Journal of Life Sciences and Agriculture",
                  volume="17", issue="6-2", pages="95-111", link="https://discover-journal.ru/jour/index.php/sjlsa/issue/view/34",
                  number="Р-Н-1538-25")
    out = {}
    for code, title, doi in (("I02.vak", "Влияние воды на прорастание семян ВАК", "10.12731/2658-6649-2025-17-6-2-1538"),
                             ("I02.white", "Влияние воды на прорастание семян БС", "https://doi.org/10.1/WL.1"),
                             ("I02.scopus", "Влияние воды на прорастание семян SCOPUS", ""),
                             ("I02.rinc", "Влияние воды на прорастание семян РИНЦ", "")):
        aid = _pub(target, owner, row_id=ach.row_by_code(code, target)["id"], title=title, doi=doi,
                   pub_date="2025-10-01", **common)
        out[code] = ach.record_line(ach.get_achievement(aid, target))
    for code in ("I02.vak", "I02.white", "I02.scopus"):
        line = out[code]
        # Russian authors with shares in author order (owner first), then Latin, (year), title, journal, vol(issue), pages
        assert line.startswith("(70%) Козырев Д.С., (10%) Поляков А., (10%) Одабашян М.Г., (5%) Иванова А.П. "), line
        assert "Kozyrev, D. S., Polyakov, A., Odabashyan, M. G., & Ivanova, A. P. (2025). Влияние воды" in line, line
        assert " Siberian Journal of Life Sciences and Agriculture, 17(6-2), 95-111" in line and "01.10.2025" not in line, line
        assert ", ссылка: https://discover-journal.ru/jour/index.php/sjlsa/issue/view/34" in line or \
            "ссылка: https://discover-journal.ru" in line, line
        assert line.endswith("Р-Н-1538-25"), line
    assert "https://doi.org/10.12731/2658-6649-2025-17-6-2-1538 " in out["I02.vak"] + " "
    assert "https://doi.org/10.1/wl.1" in out["I02.white"]
    # РИНЦ keeps its layout (nothing else restructured), but the ФИО are printed there too
    assert "Козырев Дмитрий Сергеевич" in out["I02.rinc"] and "Доля участия: 70%, 10%, 10%, 5%" in out["I02.rinc"]
    # typed reference (bib) + typed Latin authors: printed as typed, authors put in front, no duplicates
    aid = _pub(target, owner, row_id=ach.row_by_code("I02.vak", target)["id"], title="Статья со ссылкой",
               year=2025, owner_share=60, coauthors=[{"name": "Иванова Анна Петровна", "share": 40}],
               authors_lat="Kozyrev, D., & Ivanova, A.", bib="Kozyrev, D., & Ivanova, A. (2025). Статья со ссылкой. Журнал, 3(1), 5-9",
               doi="10.5/x.5")
    line = ach.record_line(ach.get_achievement(aid, target))
    assert line.startswith("(60%) Козырев Д.С., (40%) Иванова А.П. Kozyrev, D., & Ivanova, A. (2025). Статья со ссылкой. Журнал, 3(1), 5-9")
    assert line.count("Статья со ссылкой") == 1 and line.endswith("https://doi.org/10.5/x.5"), line
    # a reference that already names the main author in Russian is not prefixed a second time
    aid2 = _pub(target, owner, row_id=ach.row_by_code("I02.white", target)["id"], title="Готовая ссылка", year=2025,
                bib="Козырев Д.С. Готовая ссылка // Журнал. - 2025. - Т. 1.")
    line2 = ach.record_line(ach.get_achievement(aid2, target))
    assert line2.startswith("Козырев Д.С. Готовая ссылка") and line2.count("Козырев") == 1 and "Доля участия: 100%" in line2
    # the same lines reach the annual docx and the Excel export
    data = ach.report_data(2025, db_path=target)
    import docx as _docx
    doc = _docx.Document(io.BytesIO(annual_report.build_annual_docx(data, "", [])))
    full = "\n".join(c.text for t in doc.tables for row in t.rows for c in row.cells)
    assert out["I02.vak"] in full and out["I02.white"] in full
    assert annual_report.build_achievements_xlsx(data)
    assert ach.translit("Щукин") == "Shchukin" and ach.short_author("Иванов Иван") == "Иванов И."
    assert ach.short_author("Kozyrev, D.") == "Kozyrev, D." and ach._lat_author("Ёлкин Пётр Юрьевич") == "Elkin, P. Yu."
    for r in ach.list_achievements(owner_id=ids["o"], db_path=target):
        ach.delete_achievement(r["id"], db_path=target)
    for uid in ids.values():
        db.delete_user(uid, db_path=target)
    print("  ВАК / Белый список / Scopus: ФИО + доли + латиница + (год) название, журнал, том(вып), стр., ссылка, DOI OK")


def run_v4_refresh(target) -> None:  # noqa: ANN001
    """Item 2: admin cache reset, TTL, data stamp for the auto-refresh."""
    from sqlalchemy import event
    eng = db.get_engine(target)
    users = db.list_users(db_path=target)
    # a write by ANOTHER process (raw connection without our write watcher) is not seen until TTL / reset
    import sqlite3
    path = str(eng.url.database)
    stamp0 = db.data_stamp(target)
    con = sqlite3.connect(path)
    con.execute("INSERT INTO users (login, password_hash, full_name, role, created_at, active) "
                "VALUES ('other_proc', 'x', 'Другой Процесс', 'member', '2026-01-01T00:00:00', 1)")
    con.commit()
    con.close()
    assert not any(u["login"] == "other_proc" for u in db.list_users(db_path=target))  # stale cache
    assert db.data_stamp(target) != stamp0  # ... but the cheap stamp sees the change
    db.clear_cache()  # «Обновить данные»
    assert any(u["login"] == "other_proc" for u in db.list_users(db_path=target))
    # the TTL alone also fixes it: age the entry
    con = sqlite3.connect(path)
    con.execute("UPDATE users SET full_name = 'Другой Процесс 2' WHERE login = 'other_proc'")
    con.commit()
    con.close()
    for k, (v, t, val) in list(db._CACHE.items()):
        db._CACHE[k] = (v, t - db.CACHE_TTL - 1, val)
    assert any(u["full_name"] == "Другой Процесс 2" for u in db.list_users(db_path=target))
    assert 5 <= db.CACHE_TTL <= 600 and db._cache_ttl() == 60.0
    # the stamp query is a read: it never bumps the version and costs one statement
    n = {"q": 0}

    def _count(*_a, **_k):  # noqa: ANN002, ANN003
        n["q"] += 1
    event.listen(eng, "before_cursor_execute", _count)
    try:
        v = db.data_version(target)
        db.data_stamp(target)
        assert n["q"] == 1 and db.data_version(target) == v
    finally:
        event.remove(eng, "before_cursor_execute", _count)
    with eng.begin() as conn:
        conn.execute(text("DELETE FROM users WHERE login = 'other_proc'"))
    print("  refresh: admin cache reset, 60 s TTL, other-process writes detected by data_stamp (1 query) OK")


def run_v4_impersonation(target) -> None:  # noqa: ANN001
    """Item 3: server-side rules + full UI flow through Streamlit AppTest (cookie keeps the admin)."""
    db.init_db(db_path=target, seed_admin=True)
    admin = _admin(target)
    ma = db.create_user("imp_m1", "pass12345", "Имитов Иван Иванович", db_path=target)
    mb = db.create_user("imp_m2", "pass12345", "Отключёнов Олег Олегович", db_path=target)
    adm2 = db.create_user("imp_adm2", "pass12345", "Второй Админ", "admin", db_path=target)
    db.update_user(mb, acting_user_id=admin["id"], active=False, db_path=target)
    T = auth.impersonation_target
    assert T(admin["id"], ma, db_path=target)["login"] == "imp_m1"
    for who, whom, msg in ((ma, admin["id"], "только админ"), (admin["id"], admin["id"], "собственная"),
                           (admin["id"], mb, "отключён"), (admin["id"], adm2, "другим админом"),
                           (admin["id"], 999999, "не найден")):
        try:
            T(who, whom, db_path=target)
            raise AssertionError((who, whom))
        except auth.ImpersonationError as e:
            assert msg in str(e), (msg, str(e))
    # last-admin safeguards unchanged (impersonation adds no way around them)
    db.delete_user(adm2, acting_user_id=admin["id"], db_path=target)  # a second admin may go
    for fn in (lambda: db.delete_user(admin["id"], acting_user_id=admin["id"], db_path=target),
               lambda: db.update_user(admin["id"], acting_user_id=admin["id"], role="member", db_path=target)):
        try:
            fn()
            raise AssertionError("last admin must stay")
        except ValueError:
            pass
    _impersonation_ui(target, admin, ma)
    db.delete_user(ma, acting_user_id=admin["id"], db_path=target)
    db.delete_user(mb, acting_user_id=admin["id"], db_path=target)
    print("  impersonation: admin-only, never admin/self/disabled, last-admin rules unchanged, UI flow + cookie OK")


def _impersonation_ui(target, admin, member_id) -> None:  # noqa: ANN001
    import os
    from pathlib import Path

    from streamlit.testing.v1 import AppTest

    app_dir = str(Path(__file__).resolve().parent)
    old_url, old_pw = os.environ.get("DATABASE_URL"), os.environ.get("ADMIN_PASSWORD")
    os.environ["DATABASE_URL"] = "sqlite:///" + str(Path(target).resolve())
    os.environ.pop("ADMIN_PASSWORD", None)
    cwd = os.getcwd()
    os.chdir(app_dir)
    try:
        db.set_app_setting("sno_name", "Тест", db_path=target)
        at = AppTest.from_file(str(Path(app_dir) / "app.py"), default_timeout=90)
        at.session_state["user"] = {"id": admin["id"], "login": admin["login"], "full_name": admin["full_name"],
                                    "role": "admin"}
        at.run()
        assert not at.exception, at.exception
        assert not any("Вы вошли как" in w.value for w in at.warning)
        assert any(b.label == "Обновить данные" for b in at.sidebar.button)  # admin refresh button
        minted = []
        real_make = auth.make_auth_token
        auth.make_auth_token = lambda user, *a, **k: (minted.append(user["id"]), real_make(user, *a, **k))[1]
        at.selectbox(key="imp_pick").set_value(member_id)
        next(b for b in at.button if b.key == "imp_go").click().run()
        assert not at.exception, at.exception
        assert at.session_state["user"]["id"] == member_id and at.session_state["_imp_admin"]["id"] == admin["id"]
        assert any("Вы вошли как" in w.value and "Имитов" in w.value for w in at.warning)
        assert any(t.value == "Мои достижения" for t in at.title)  # the member's cabinet
        assert not any(b.label == "Обновить данные" for b in at.sidebar.button)  # member view: no admin button
        assert minted == []  # no cookie is (re)issued while impersonating: it keeps the admin identity
        # a write during impersonation is attributed to the member
        K = ach.kind_by_code("volunteer", target)
        ach.save_achievement({"kind_id": K["id"], "row_id": ach.row_by_code("I15.reg", target)["id"],
                              "title": "Волонтёрство под имитацией", "date_from": "2026-05-05"},
                             at.session_state["user"], db_path=target)
        rec = next(r for r in ach.list_achievements(owner_id=member_id, db_path=target))
        assert rec["owner_id"] == member_id and rec["created_by"] == member_id
        back = next(b for b in at.button if b.label == "Вернуться в админа" and b.key == "imp_return")
        back.click().run()
        assert not at.exception, at.exception
        assert at.session_state["user"]["id"] == admin["id"] and at.session_state["user"]["role"] == "admin"
        assert "_imp_admin" not in at.session_state
        assert not any("Вы вошли как" in w.value for w in at.warning)
        assert minted == []  # ... and none on the way back either
        auth.make_auth_token = real_make
        assert any(t.value == "Панель лидера СНО" for t in at.title)
        # the admin refresh button clears the caches
        db.list_users(db_path=target)
        assert any(k[0].endswith(Path(target).name) for k in db._CACHE)
        next(b for b in at.sidebar.button if b.label == "Обновить данные").click().run()
        assert not at.exception, at.exception
        assert any("Данные обновлены" in s.value for s in at.sidebar.success)
        # a plain member session (no admin) cannot see the impersonation UI
        at2 = AppTest.from_file(str(Path(app_dir) / "app.py"), default_timeout=90)
        at2.session_state["user"] = {"id": member_id, "login": "imp_m1", "full_name": "Имитов Иван Иванович",
                                     "role": "member"}
        at2.run()
        assert not at2.exception and not any(b.label == "Войти как участник" for b in at2.button)
        assert not any(b.label == "Обновить данные" for b in at2.sidebar.button)
        # forged impersonation state in a member session is refused: role is re-read from the DB
        at2.session_state["_imp_admin"] = {"id": member_id, "login": "imp_m1", "full_name": "x", "role": "admin"}
        at2.run()
        assert at2.session_state["user"] is None, "forged _imp_admin must log out"
    finally:
        os.chdir(cwd)
        if "real_make" in dir():
            auth.make_auth_token = real_make
        for k, v in (("DATABASE_URL", old_url), ("ADMIN_PASSWORD", old_pw)):
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        db.get_engine(target)
        for r in ach.list_achievements(owner_id=member_id, db_path=target):
            ach.delete_achievement(r["id"], db_path=target)


def run_all(target, fresh) -> None:  # noqa: ANN001
    """target: DB for catalog/fixture tests (fresh); fresh(): returns a new empty DB target."""
    db.init_db(db_path=target, seed_admin=True)
    run_catalog(target)
    run_cache(target)
    run_numbers_and_validation(target)
    run_orphans_and_meetings(target)
    run_fixtures_report(target)
    run_migration(fresh())
    run_coauthors(fresh())
    run_v4_pubdate(fresh())
    run_v4_bibformat(fresh())
    run_v4_refresh(fresh())
    run_v4_impersonation(fresh())
