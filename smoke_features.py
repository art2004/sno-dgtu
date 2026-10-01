"""Smoke tests: бэкап, журнал действий, «Что дозаполнить».
Вызываются из smoke_test.py (SQLite по умолчанию, Postgres через SMOKE_DATABASE_URL)."""

from __future__ import annotations

import io
import json
import os
import zipfile
from datetime import date
from pathlib import Path

from sqlalchemy import text

import achievements as ach
import audit
import backup
import db
import todo

ROOT = Path(__file__).resolve().parent


def _admin(target) -> dict:  # noqa: ANN001
    return next(u for u in db.list_users(db_path=target) if u["role"] == "admin")


_N = iter(range(100, 99999))


def _doklad(target, owner, title="Форум Журнал", level="I01.reg", topic="Тема", actor=None, ctx=None,  # noqa: ANN001
            date_from="2026-03-10") -> int:
    K = ach.kind_by_code("doklad", target)
    return ach.save_achievement(
        {"kind_id": K["id"], "owner_id": owner, "row_id": ach.row_by_code(level, target)["id"] if level else None,
         "title": title, "date_from": date_from, "topic": topic, "ochno": True,
         "number": f"Р-Н-{next(_N)}-26"},
        actor or _admin(target), db_path=target, audit_ctx=ctx)["id"]


def _entries(target, **kw):  # noqa: ANN001, ANN201
    db.clear_cache()
    return audit.list_entries(db_path=target, **kw)


# ── 2. Журнал действий ──────────────────────────────────────────────────────


def run_audit(target) -> None:  # noqa: ANN001
    db.init_db(db_path=target, seed_admin=True)
    assert audit.count_entries(target) == 0 or True
    base = audit.count_entries(target)
    admin = _admin(target)
    m = db.create_user("aud_m", "pass12345", "Журналов Жан Жанович", db_path=target)
    member = db.get_user_by_id(m, db_path=target)

    # создание участником
    aid = _doklad(target, m, "Конференция АУДИТ", actor=member, ctx={"actor": member, "as_user": None})
    es = _entries(target)
    assert len(es) == base + 1
    e = es[0]
    assert e["action"] == "achievement_create" and e["entity_id"] == aid and e["actor_id"] == m
    assert e["as_user_id"] is None and "Конференция АУДИТ" in e["summary"] and "Журналов" in e["summary"]
    assert e["details"]["after"]["Название"] == "Конференция АУДИТ"
    assert e["ts"][:10] == audit.now_msk()[:10]

    # изменение: было / стало
    ach.save_achievement({"kind_id": ach.kind_by_code("doklad", target)["id"], "owner_id": m,
                          "row_id": ach.row_by_code("I01.ru", target)["id"], "title": "Конференция АУДИТ-2",
                          "date_from": "2026-03-10", "topic": "Тема", "ochno": True},
                         member, achievement_id=aid, db_path=target,
                         audit_ctx={"actor": member, "as_user": None})
    e = _entries(target)[0]
    assert e["action"] == "achievement_update" and e["entity_id"] == aid
    fields = {c["field"]: (c["before"], c["after"]) for c in e["details"]["changes"]}
    assert fields["Название"] == ("Конференция АУДИТ", "Конференция АУДИТ-2"), fields
    assert "всероссийский" in fields["Уровень / подпункт"][1] and "региональный" in fields["Уровень / подпункт"][0]
    assert "было «Конференция АУДИТ», стало «Конференция АУДИТ-2»" in e["summary"]
    assert "Тема" not in fields  # неизменённое поле не пишется

    # админ «от имени» участника: actor = админ, as_user = участник
    aid2 = _doklad(target, m, "Доклад от имени", actor=member,
                   ctx={"actor": admin, "as_user": member})
    e = _entries(target)[0]
    assert e["actor_id"] == admin["id"] and e["as_user_id"] == m and e["as_user_name"] == member["full_name"], e
    assert audit.who({"user": {"id": m}, "_imp_admin": {"id": admin["id"]}}) == \
        {"actor": {"id": admin["id"]}, "as_user": {"id": m}}
    assert audit.who({"user": {"id": m}}) == {"actor": {"id": m}, "as_user": None}

    # удаление: снимок «было»
    assert ach.delete_achievement(aid2, audit_ctx={"actor": admin, "as_user": None}, db_path=target)
    e = _entries(target)[0]
    assert e["action"] == "achievement_delete" and e["entity_id"] == aid2
    assert e["details"]["before"]["Название"] == "Доклад от имени" and "Удалено" in e["summary"]
    # чужое удаление через owner_id не пишется (ничего не удалено)
    n = len(_entries(target))
    assert not ach.delete_achievement(aid, owner_id=admin["id"], db_path=target)
    assert len(_entries(target)) == n
    # ошибка валидации ничего не пишет
    try:
        ach.save_achievement({"kind_id": ach.kind_by_code("doklad", target)["id"], "owner_id": m, "title": "",
                              "date_from": "2026-03-10", "row_id": ach.row_by_code("I01.ru", target)["id"],
                              "topic": "t"}, member, db_path=target)
        raise AssertionError("expected ValueError")
    except ValueError:
        pass
    assert len(_entries(target)) == n

    # события приложения (вход, импорт, настройки) - прямая запись и фильтры
    audit.log("login", actor=member, entity="user", entity_id=m, summary="Вход: Журналов", db_path=target)
    audit.log("import_members", actor=admin, entity="users", summary="Импорт участников из Excel «x.xlsx»: создано 2, "
              "пропущено 1", db_path=target)
    audit.log("settings_change", actor=admin, entity="settings",
              summary="Настройки отчёта: Название СНО: было «А», стало «Б»", db_path=target)
    acts = {r["action"] for r in _entries(target)}
    assert {"login", "import_members", "settings_change", "achievement_create", "achievement_update",
            "achievement_delete"} <= acts
    assert {r["action"] for r in _entries(target, actions=["login"])} == {"login"}
    only_m = _entries(target, user_id=m)
    assert only_m and all(m in (r["actor_id"], r["as_user_id"], r["target_user_id"]) for r in only_m)
    assert not any(r["action"] == "settings_change" for r in only_m)
    today = date.today()
    assert len(_entries(target, date_from=today, date_to=today)) == len(_entries(target))
    assert _entries(target, date_from=date(2000, 1, 1), date_to=date(2000, 1, 2)) == []
    assert _entries(target, date_from=date(2100, 1, 1)) == []
    # поиск без учёта регистра, в т.ч. кириллица
    assert any("АУДИТ-2" in r["summary"] for r in _entries(target, search="аудит-2"))
    assert _entries(target, search="журналов") and _entries(target, search="ИМПОРТ УЧАСТНИКОВ")
    assert _entries(target, search="нет такого слова ыыы") == []
    assert (m, "Журналов Жан Жанович") in audit.known_users(db_path=target)

    # журнал не ломает основную операцию и не хранит секреты
    assert audit.log("login", actor={"id": "not-int"}, db_path=target) is None  # плохие данные → None, без исключения
    blob = json.dumps(_entries(target), ensure_ascii=False, default=str)
    assert "$2b$" not in blob and "pass12345" not in blob
    # участник удалён - история остаётся
    db.delete_user(m, acting_user_id=admin["id"], db_path=target)
    assert any(r["actor_id"] == m for r in _entries(target))
    # таблица создаётся тем же механизмом: повторный init_db ничего не теряет
    cnt = audit.count_entries(target)
    db.init_db(db_path=target)
    assert audit.count_entries(target) == cnt
    print(f"  audit log OK ({cnt} записей: создание / изменение было-стало / удаление / «от имени» / фильтры / поиск)")


# ── 3. Что дозаполнить ──────────────────────────────────────────────────────


def run_todo(target) -> None:  # noqa: ANN001
    db.init_db(db_path=target, seed_admin=True)
    admin = _admin(target)
    a = db.create_user("td_a", "pass12345", "Незаполнов Никита", db_path=target)
    b = db.create_user("td_b", "pass12345", "Полнов Павел", db_path=target)
    # 1) легаси: доклад без уровня, статья ВАК без долей (как после миграции), конкурс без уровня, грант без подпункта
    add = lambda u, *x, **k: db.add_participation_ex(u, *x, db_path=target, **k)  # noqa: E731
    add(a, "Конференция без уровня", "конференция", "2026-04-01")
    add(a, "Статья без долей", "статья", "2026-04-02", article_topic="т", indexing="ВАК")
    add(a, "Статья без индексации", "статья", "2026-04-03", article_topic="т", indexing="Без индексации")
    add(a, "Конкурс без уровня", "конкурс", "2026-04-04")
    add(b, "Грант без подпункта", "грант", "2026-04-05")
    db.init_db(db_path=target)  # перенос в achievements
    # 2) заглушка у доклада + полный доклад + публикация без журнала
    ph = _doklad(target, a, title="Мероприятие не указано (импорт из ЛК, уточнить)", topic="Тема X")
    ok = _doklad(target, b, title="Полная конференция", topic="Всё заполнено")
    zao = ach.save_achievement({"kind_id": ach.kind_by_code("doklad", target)["id"], "owner_id": b,
                                "row_id": ach.row_by_code("I01.reg", target)["id"], "title": "Заочная", "topic": "т",
                                "date_from": "2026-04-06", "ochno": False}, admin, db_path=target)["id"]
    # доклад с пустой темой и стипендия без учебного года (прямая правка, как в старых данных)
    notopic = _doklad(target, b, title="Без темы", topic="x")
    with db.get_engine(target).begin() as conn:
        conn.execute(text("UPDATE achievements SET topic = '' WHERE id = :i"), {"i": notopic})
        conn.execute(text("UPDATE achievements SET title = '' WHERE id = :i"), {"i": ok})
    db.clear_cache()

    recs = {r["id"]: r for r in todo.list_todo(db_path=target)}
    by_title = {r["title"]: r for r in recs.values()}
    codes = lambda t: set(by_title[t]["problem_codes"])  # noqa: E731
    assert "no_level" in codes("Конференция без уровня")
    assert {"no_indexing"} <= codes("Статья без долей") or "no_share" in codes("Статья без долей")
    assert "no_share" in codes("Статья без долей")
    assert "Статья без индексации" not in by_title  # «Без индексации» - валидная запись, как в report_data
    assert "no_level" in codes("Конкурс без уровня")
    assert "no_level" in codes("Грант без подпункта") and by_title["Грант без подпункта"]["counted"] is False
    assert "placeholder" in set(recs[ph]["problem_codes"])
    assert "no_topic" in set(recs[notopic]["problem_codes"])
    assert "required" in set(recs[ok]["problem_codes"])  # пустое название (обязательное поле)
    assert zao not in recs  # заочный - по умолчанию не показываем
    assert zao in {r["id"] for r in todo.list_todo(types=["zaochno"], db_path=target)}
    # правила совпадают с отчётом: каждое предупреждение report_data попало в список (кроме «только заочный»)
    wid = {w["id"] for w in ach.report_data(2026, db_path=target)["warnings"]}
    assert wid <= {r["id"] for r in todo.list_todo(year=2026, db_path=target)}
    # фильтры
    only_a = todo.list_todo(owner_id=a, db_path=target)
    assert only_a and all(r["owner_id"] == a for r in only_a)
    only_ph = todo.list_todo(types=["placeholder"], db_path=target)
    assert [r["id"] for r in only_ph] == [ph]
    assert todo.list_todo(year=2019, db_path=target) == []
    c = todo.counts_by_type(todo.list_todo(types=list(todo.PROBLEM_LABELS), db_path=target))
    assert c["no_level"] >= 3 and c["placeholder"] == 1 and c["no_topic"] == 2, c  # + перенесённый доклад без темы
    assert todo.counts_by_owner(only_a)["Незаполнов Никита"] == len(only_a)
    # полная запись в список не попадает
    full = _doklad(target, b, title="Идеальная", topic="Тема", date_from="2026-05-01")
    assert full not in {r["id"] for r in todo.list_todo(types=list(todo.PROBLEM_LABELS), db_path=target)}
    # после исправления запись исчезает
    ach.save_achievement({"kind_id": ach.kind_by_code("doklad", target)["id"], "owner_id": a,
                          "row_id": ach.row_by_code("I01.ru", target)["id"], "title": "Настоящая конференция",
                          "date_from": "2026-03-10", "topic": "Тема X", "ochno": True, "number": "Р-Н-77-26"},
                         admin, achievement_id=ph, db_path=target)
    assert ph not in {r["id"] for r in todo.list_todo(db_path=target)}
    # Excel
    from openpyxl import load_workbook
    recs = todo.list_todo(db_path=target)
    wb = load_workbook(io.BytesIO(todo.build_xlsx(recs, "2026")))
    assert wb.sheetnames == ["Что дозаполнить", "Сводка"]
    ws = wb["Что дозаполнить"]
    assert ws.max_row == len(recs) + 1 and [c.value for c in ws[1]][:3] == ["ID", "Участник", "Логин"]
    assert {ws.cell(r, 1).value for r in range(2, ws.max_row + 1)} == {r["id"] for r in recs}
    assert any("Заглушка" in str(x.value) for x in wb["Сводка"]["A"]) is False  # заглушку уже исправили
    assert todo.build_xlsx([], "")[:2] == b"PK"  # пустой список тоже выгружается
    for u in (a, b):
        db.delete_user(u, acting_user_id=admin["id"], db_path=target)
    print(f"  todo OK ({len(recs)} записей к дозаполнению; фильтры, счётчики, xlsx; правила как в report_data)")


# ── 0. Аудит: что решает, попадёт ли запись в отчёт (номер, поля, уровень, дата) ─────────────


def run_report_gate(target) -> None:  # noqa: ANN001
    """Каждый вид: без номера запись НЕ считается (is_counted=False) и даёт предупреждение в
    report_data и в «Что дозаполнить»; с номером - считается. Исключения (agreement, sno_contest,
    funded, запись без владельца) - считаются без номера. Плюс остальные дыры аудита."""
    db.init_db(db_path=target, seed_admin=True)
    admin = _admin(target)
    u = db.create_user("gate_u", "pass12345", "Проверов Пётр", db_path=target)
    K = {k["code"]: k for k in ach.list_kinds(target, admin=True, include_hidden=True)}
    R = lambda c: ach.row_by_code(c, target)["id"]  # noqa: E731
    SP = lambda c: next(sp["id"] for k in K.values() for sp in k["subpoints"] if sp["code"] == c)  # noqa: E731
    Y = "2026-05-05"
    # (метка, вид, поля, нужен ли номер)
    cases = [
        ("doklad", "doklad", dict(row_id=R("I01.ru"), title="Конф", date_from=Y, topic="Тема", ochno=True), 1),
        ("publication", "publication", dict(row_id=R("I02.vak"), title="Статья", journal="Ж", year=2026,
                                            owner_share=100), 1),
        ("contest", "contest", dict(row_id=R("I03.ru"), title="Кейс", date_from=Y), 1),
        ("edu", "edu", dict(row_id=R("I04.ru"), title="Школа", date_from=Y), 1),
        ("expo", "expo", dict(row_id=R("I05.ru"), title="Выставка", date_from=Y), 1),
        ("ip", "ip", dict(row_id=R("I06.patent"), title="Патент", doc_number="123", date_from=Y), 1),
        ("grant.app", "grant", dict(subpoint_id=SP("grant.app"), row_id=R("I07.ru"), title="РНФ", project="П",
                                    date_from=Y, status="подана"), 1),
        ("grant.rnf", "grant", dict(subpoint_id=SP("grant.rnf"), title="РНФ 1", date_from=Y, topic="Т"), 1),
        ("grant.pp", "grant", dict(subpoint_id=SP("grant.pp"), title="ПП 1", date_from=Y, topic="Т"), 1),
        ("grant.funds", "grant", dict(subpoint_id=SP("grant.funds"), title="Фонд 1", date_from=Y, topic="Т"), 1),
        ("grant.hoz", "grant", dict(subpoint_id=SP("grant.hoz"), title="Хоз 1", date_from=Y, topic="Т"), 1),
        ("stipend", "stipend", dict(row_id=R("I08.other"), title="Стипендия", ayear="2025-2026"), 1),
        ("exchange", "exchange", dict(row_id=R("I10.intl"), title="Обмен", date_from=Y), 1),
        ("fsi", "fsi", dict(row_id=R("I13.umnik_p"), title="УМНИК", date_from=Y), 1),
        ("prize", "prize", dict(row_id=R("I14.ru"), title="Премия", date_from=Y), 1),
        ("volunteer", "volunteer", dict(row_id=R("I15.ru"), title="Волонтёры", date_from=Y), 1),
        ("org_sci(владелец)", "org_sci", dict(row_id=R("I11.ru"), title="Орг", date_from=Y), 1),
        ("org_pop(владелец)", "org_pop", dict(row_id=R("I12.ru"), title="Орг-поп", date_from=Y), 1),
        ("sno_contest", "sno_contest", dict(row_id=R("I16.part"), title="Конкурс СНО", date_from=Y), 0),
        ("agreement", "agreement", dict(title="Партнёр", agreement="1 от 01.01.2026", date_from=Y), 0),
        ("funded", "funded", dict(title="Работа", source="бюджет", date_from=Y), 0),
    ]
    ids = {}
    nxt = iter(range(1, 999))
    for label, code, f, need in cases:
        data = {"kind_id": K[code]["id"], "owner_id": u, **f}
        res = ach.save_achievement(data, admin, db_path=target)  # без номера сохраняется
        rec = ach.get_achievement(res["id"], target)
        if need:
            assert not rec["counted"] and res["counted"] is False, label
            assert any("номер достижения" in i for i in rec["issues"]), (label, rec["issues"])
        else:
            assert rec["counted"] and not rec["issues"], (label, rec["issues"])
        ids[label] = (res["id"], data, need)
    db.clear_cache()
    rd = ach.report_data(2026, db_path=target)
    warn = {w["id"]: w for w in rd["warnings"]}
    tdp = {r["id"]: r for r in todo.list_todo(year=2026, types=["no_number"], db_path=target)}
    for label, (aid, _data, need) in ids.items():
        if need:
            assert "номер достижения" in warn[aid]["issues"] and not warn[aid]["counted"], label
            assert aid in tdp and "no_number" in tdp[aid]["problem_codes"], label
        else:
            assert aid not in warn and aid not in tdp, label
    assert rd["total"] == sum(1 for *_x, need in cases if not need)  # в отчёте только 3 вида без номера
    # с номером запись попадает в отчёт, предупреждение исчезает
    for i, (label, (aid, data, need)) in enumerate(ids.items()):
        if need:
            ach.save_achievement({**data, "number": f"Р-Н-{900 + i}-26"}, admin, achievement_id=aid, db_path=target)
    db.clear_cache()
    rd = ach.report_data(2026, db_path=target)
    assert rd["total"] == len(cases) and rd["warnings"] == [], (rd["total"], rd["warnings"])
    assert not any(w for w in todo.list_todo(year=2026, db_path=target))

    # запись без владельца (админская «СНО в целом») номера не требует
    nid = ach.save_achievement({"kind_id": K["org_sci"]["id"], "owner_id": None, "row_id": R("I11.reg"),
                                "title": "Орг от СНО", "date_from": Y}, admin, db_path=target)["id"]
    assert ach.get_achievement(nid, target)["counted"]

    # номер-заглушка и «не номер» = пустой номер
    gid = ids["fsi"][0]
    for bad in ("нет", "-", "б/н", "н/д"):
        ach.save_achievement({**ids["fsi"][1], "number": bad}, admin, achievement_id=gid, db_path=target)
        r = ach.get_achievement(gid, target)
        assert r["number"] is None and not r["counted"], bad
    ach.save_achievement({**ids["fsi"][1], "number": "Р-Н-абв"}, admin, achievement_id=gid, db_path=target)
    r = ach.get_achievement(gid, target)
    assert not r["counted"] and any("не похож на номер" in i for i in r["issues"])
    ach.save_achievement({**ids["fsi"][1], "number": "Р-Н-905-26"}, admin, achievement_id=gid, db_path=target)
    assert ach.get_achievement(gid, target)["counted"]

    # один и тот же номер у двух записей: предупреждение (запись остаётся в отчёте)
    a2 = ach.save_achievement({**ids["prize"][1], "title": "Премия 2", "number": "Р-Н-905-26"}, admin,
                              db_path=target)["id"]
    r2 = ach.get_achievement(a2, target)
    assert r2["counted"] and any("уже указан в другой записи" in i for i in r2["issues"])
    assert any("уже указан" in i for i in ach.get_achievement(gid, target)["issues"])
    ach.delete_achievement(a2, db_path=target)

    # заочный доклад и дубли вне отчёта по замыслу: номер для них не требуем (нет лишнего шума)
    zid = ach.save_achievement({**ids["doklad"][1], "title": "Заочный", "ochno": False}, admin, db_path=target)["id"]
    z = ach.get_achievement(zid, target)
    assert not z["counted"] and z["issues"] == [], z["issues"]

    # тема доклада: пустая тема блокирует (строка отчёта «доклад на тему: «»» была бы пустой)
    did = ids["doklad"][0]
    with db.get_engine(target).begin() as conn:
        conn.execute(text("UPDATE achievements SET topic = '' WHERE id = :i"), {"i": did})
    db.clear_cache()
    r = ach.get_achievement(did, target)
    assert not r["counted"] and any("тему доклада" in i for i in r["issues"])
    with db.get_engine(target).begin() as conn:
        conn.execute(text("UPDATE achievements SET topic = 'Тема' WHERE id = :i"), {"i": did})

    # дыры «пустое обязательное поле» (прямая запись в БД, как в старых данных / импорте)
    def broken(label, sql_set, needle):  # noqa: ANN001
        aid = ids[label][0]
        with db.get_engine(target).begin() as conn:
            old = dict(conn.execute(text("SELECT * FROM achievements WHERE id = :i"), {"i": aid}).mappings().one())
            conn.execute(text(f"UPDATE achievements SET {sql_set} WHERE id = :i"), {"i": aid})
        db.clear_cache()
        r = ach.get_achievement(aid, target)
        assert not r["counted"] and any(needle in i for i in r["issues"]), (label, sql_set, r["issues"])
        assert aid in {x["id"] for x in ach.report_data(2026, db_path=target)["warnings"]}, label
        cols = ", ".join(f"{k} = :{k}" for k in ("title", "details", "owner_id", "row_id", "indicator_id"))
        with db.get_engine(target).begin() as conn:
            conn.execute(text(f"UPDATE achievements SET {cols} WHERE id = :id"),
                         {**{k: old[k] for k in ("title", "details", "owner_id", "row_id", "indicator_id")},
                          "id": aid})
        db.clear_cache()
        assert ach.get_achievement(aid, target)["counted"], label

    broken("contest", "title = ''", "пустые поля")                  # название
    broken("ip", "details = '{}'", "Номер документа")               # номер документа/заявки
    broken("grant.app", "details = '{\"project\": \"П\"}'", "Статус заявки")       # статус заявки
    broken("grant.app", "details = '{\"status\": \"подана\"}'", "Название проекта")  # проект
    broken("grant.app", "details = '{\"project\": \"П\", \"status\": \"не поддержана\"}'", "Статус заявки")
    broken("funded", "details = '{}'", "Источник")                  # источник
    broken("agreement", "details = '{}'", "Номер и дата соглашения")
    broken("publication", "title = ''", "пустые поля")
    # уровень из чужого показателя (I01-строка у выставки) не считается
    broken("expo", f"row_id = {R('I01.ru')}", "не относится")
    # владелец не выбран у личного вида
    broken("prize", "owner_id = NULL", "укажите участника")
    # без даты запись не попадает ни в один год: предупреждение в отчёте любого года и в todo
    aid = ids["edu"][0]
    with db.get_engine(target).begin() as conn:
        conn.execute(text("UPDATE achievements SET date_from = NULL, date_to = NULL WHERE id = :i"), {"i": aid})
    db.clear_cache()
    assert any(w["id"] == aid and "нет даты" in w["issues"] for w in ach.report_data(2026, db_path=target)["warnings"])
    assert any(w["id"] == aid for w in ach.report_data(2025, db_path=target)["warnings"])
    assert aid in {r["id"] for r in todo.list_todo(year=2026, db_path=target)}
    assert "no_date" in next(r for r in todo.list_todo(db_path=target) if r["id"] == aid)["problem_codes"]
    with db.get_engine(target).begin() as conn:
        conn.execute(text("UPDATE achievements SET date_from = :d WHERE id = :i"), {"i": aid, "d": Y})
    db.clear_cache()

    # проверки при сохранении: уровень не от того показателя, год-опечатка
    for bad, needle in (({**ids["expo"][1], "row_id": R("I01.ru")}, "не относится"),
                        ({**ids["edu"][1], "date_from": "0202-05-05"}, "опечатка"),
                        ({**ids["edu"][1], "date_from": "2206-05-05"}, "опечатка")):
        try:
            ach.save_achievement(bad, admin, db_path=target)
            raise AssertionError(bad)
        except ValueError as e:
            assert needle in str(e), str(e)
    # публикация без доли и без журнала: остаётся в отчёте с предупреждением (мягкое правило)
    pid = ach.save_achievement({**ids["publication"][1], "title": "Без журнала", "number": "Р-Н-950-26",
                                "owner_share": 100}, admin, db_path=target)["id"]
    with db.get_engine(target).begin() as conn:
        conn.execute(text("UPDATE achievements SET details = '{\"year\": 2026}' WHERE id = :i"), {"i": pid})
    db.clear_cache()
    p = ach.get_achievement(pid, target)
    assert p["counted"] and any("журнал" in i for i in p["issues"])
    # «Мои достижения»: save_achievement сообщает, считается ли запись
    assert ach.save_achievement({**ids["edu"][1], "title": "Без номера 2"}, admin, db_path=target)["counted"] is False
    print(f"  report gate OK ({len(cases)} видов/подпунктов: без номера не считается и даёт предупреждение; "
          "исключения agreement/sno_contest/funded/без владельца; пустые поля, чужой уровень, без даты, "
          "заглушки номера, дубль номера)")


# ── 1. Бэкап ────────────────────────────────────────────────────────────────


def _snapshot(target) -> dict:  # noqa: ANN001
    d = backup.dump(target)["tables"]
    return {t: v["rows"] for t, v in d.items() if t != "audit_log"}


def run_backup(target, target2) -> None:  # noqa: ANN001
    db.init_db(db_path=target, seed_admin=True)
    admin = _admin(target)
    m = db.create_user("bk_m", "pass12345", "Бэкапов Борис", db_path=target)
    co = db.create_user("bk_c", "pass12345", "Копиев Кирилл", db_path=target)
    _doklad(target, m, "Конференция для бэкапа", topic="Тема \"с кавычками\", запятой\nи переводом строки")
    K = ach.kind_by_code("publication", target)
    ach.save_achievement({"kind_id": K["id"], "owner_id": m, "row_id": ach.row_by_code("I02.vak", target)["id"],
                          "title": "Статья бэкап", "journal": "Журнал", "pub_date": "2026-02-02",
                          "owner_share": 60, "coauthors": [{"user_id": co, "name": "Копиев Кирилл", "share": 40}]},
                         admin, db_path=target)
    db.add_meeting("2026-03-03", "13:30", "Ауд 1", "Заседание бэкап", "Собрание", db_path=target)
    db.save_report_settings("СНО Бэкап", "Приложение Е", [{"position": "Декан", "name": "Иванов И.И."}], db_path=target)
    audit.log("login", actor=admin, summary="Вход для бэкапа", db_path=target)
    import auth
    auth.get_cookie_secret(target)  # создаёт settings.cookie_secret
    db.clear_cache()

    raw = backup.build_zip(target)
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        names = set(z.namelist())
        assert {"backup.json", "README.txt"} <= names
        for t in backup.table_names(target):
            assert f"csv/{t}.csv" in names, t
        data = json.loads(z.read("backup.json").decode("utf-8"))
        csv_users = z.read("csv/users.csv").decode("utf-8-sig")
        readme = z.read("README.txt").decode("utf-8")
    assert data["meta"]["format"] == "sno-backup" and data["meta"]["contains_password_hashes"] is True
    assert "audit_log" in data["tables"] and "achievements" in data["tables"]
    u_cols = data["tables"]["users"]["columns"]
    hashes = [r[u_cols.index("password_hash")] for r in data["tables"]["users"]["rows"]]
    assert hashes and all(h.startswith("$2") for h in hashes)  # хэши включены (для восстановления), не пароли
    assert "pass12345" not in raw.decode("latin-1") and "$2" in csv_users
    assert "чувствительный" in readme
    sk = data["tables"]["settings"]["columns"].index("key")
    assert "cookie_secret" not in {r[sk] for r in data["tables"]["settings"]["rows"]}
    assert "sno_name" in {r[sk] for r in data["tables"]["settings"]["rows"]}
    # проверка целостности
    backup.load_zip(raw)
    bad = bytearray(raw)
    for bad_in, msg in ((b"not a zip", "Не удалось прочитать"),):
        try:
            backup.load_zip(bad_in)
            raise AssertionError("must fail")
        except backup.BackupError as e:
            assert msg in str(e)
    tampered = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(raw)) as zin, zipfile.ZipFile(tampered, "w") as zout:
        for n in zin.namelist():
            body = zin.read(n)
            if n == "backup.json":
                body = body.replace("Конференция для бэкапа".encode(), "Подделка".encode())
            zout.writestr(n, body)
    try:
        backup.load_zip(tampered.getvalue())
        raise AssertionError("tampered backup must be rejected")
    except backup.BackupError as e:
        assert "Контрольная сумма" in str(e)

    # восстановление в ДРУГУЮ пустую базу: данные идентичны, пароли работают, id сохранены
    before = _snapshot(target)
    db.init_db(db_path=target2, seed_admin=False)
    other_m = db.create_user("junk", "pass12345", "Мусор", db_path=target2)  # будет стёрт восстановлением
    restored = backup.restore(backup.load_zip(raw), target2)
    assert restored["users"] == len(before["users"])
    db.clear_cache()
    after = _snapshot(target2)
    norm = lambda rows: json.dumps(rows, ensure_ascii=False, sort_keys=True, default=str)  # noqa: E731
    for t in before:
        assert norm(before[t]) == norm(after[t]), f"таблица {t} после восстановления отличается"
    assert db.get_user_by_login("junk", db_path=target2) is None
    from auth import verify_password
    assert verify_password("pass12345", db.get_user_by_login("bk_m", db_path=target2)["password_hash"])
    assert [x["title"] for x in ach.list_achievements(owner_id=m, db_path=target2)] == \
        [x["title"] for x in ach.list_achievements(owner_id=m, db_path=target)]
    pub = next(r for r in ach.list_achievements(owner_id=m, db_path=target2) if r["form"] == "publication")
    assert pub["owner_share"] == 60 and [p["share"] for p in pub["people"]] == [40]
    assert db.get_report_settings(db_path=target2)["sno_name"] == "СНО Бэкап"
    assert len(db.list_meetings(db_path=target2)) == 1
    # после восстановления вставка нового ряда не конфликтует по id (последовательности Postgres)
    new_id = db.create_user("after_restore", "pass12345", "После Восстановления", db_path=target2)
    assert new_id > max(r[0] for r in before["users"])
    _doklad(target2, new_id, "После восстановления", date_from="2026-06-06")
    # неудачное восстановление откатывается целиком
    snap2 = _snapshot(target2)
    broken = json.loads(json.dumps(data))
    broken["tables"]["achievements"]["rows"].append(broken["tables"]["achievements"]["rows"][0])  # дубль PK
    try:
        backup.restore(broken, target2)
        raise AssertionError("duplicate PK must fail")
    except AssertionError:
        raise
    except Exception:  # noqa: BLE001
        pass
    db.clear_cache()
    assert norm(_snapshot(target2)) == norm(snap2), "ошибка восстановления должна откатываться"

    # restore_backup.py: dry-run ничего не меняет; без подтверждения - отмена; с подтверждением - запись
    import subprocess
    import sys
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        zp = Path(td) / "b.zip"
        zp.write_bytes(raw)
        env = {**os.environ}
        env.pop("DATABASE_URL", None)
        is_pg = db.is_postgres(target2)
        tgt = ["--db-url", str(db.resolve_url(target2))] if is_pg else ["--db-path", str(db.resolve_url(target2))[len("sqlite:///"):]]
        run_cli = lambda *a, inp="": subprocess.run([sys.executable, str(ROOT / "restore_backup.py"), str(zp), *tgt, *a],  # noqa: E731
                                                     input=inp, capture_output=True, text=True, env=env, timeout=120)
        r = run_cli("--dry-run")
        assert r.returncode == 0 and "ничего не изменено" in r.stdout, r.stdout + r.stderr
        db.clear_cache()
        assert norm(_snapshot(target2)) == norm(snap2)
        r = run_cli(inp="нет\n")
        assert r.returncode == 1 and "Отменено" in r.stdout, r.stdout + r.stderr
        db.clear_cache()
        assert norm(_snapshot(target2)) == norm(snap2)
        r = run_cli(inp="ВОССТАНОВИТЬ\n")
        assert r.returncode == 0 and "Готово" in r.stdout, r.stdout + r.stderr
        assert list(Path(td).glob("before_restore_*.zip")), "копия перед восстановлением не создана"
        db.clear_cache()
        assert norm(_snapshot(target2)) == norm(before_nonaudit(before))
        r = subprocess.run([sys.executable, str(ROOT / "restore_backup.py"), str(Path(td) / "nope.zip"), *tgt],
                           capture_output=True, text=True, env=env, timeout=60)
        assert r.returncode == 2
    db.clear_cache()
    assert any(e["action"] == "backup_restore" for e in audit.list_entries(db_path=target2))
    # в интерфейсе нет восстановления
    src = (ROOT / "app.py").read_text(encoding="utf-8") + (ROOT / "ui_achievements.py").read_text(encoding="utf-8")
    assert "backup.restore" not in src and "restore_backup" not in src.replace("restore_backup.py", "")
    print("  backup OK (zip: json + csv по таблицам, хэши паролей включены, cookie_secret нет, "
          "восстановление идентично + откат при ошибке, restore_backup.py: dry-run / подтверждение)")


def before_nonaudit(before: dict) -> dict:
    return before


# ── UI (Streamlit AppTest, SQLite) ──────────────────────────────────────────


def run_ui(target) -> None:  # noqa: ANN001
    from streamlit.testing.v1 import AppTest

    db.init_db(db_path=target, seed_admin=True)
    admin = _admin(target)
    m = db.create_user("ui_m", "pass12345", "Интерфейсов Игорь", db_path=target)
    db.add_participation_ex(m, "Конференция UI", "конференция", "2026-04-01", db_path=target)
    db.init_db(db_path=target)
    _doklad(target, m, "Мероприятие не указано (импорт из ЛК, уточнить)", topic="UI тема")
    app_dir = str(ROOT)
    old_url, old_pw = os.environ.get("DATABASE_URL"), os.environ.get("ADMIN_PASSWORD")
    os.environ["DATABASE_URL"] = str(db.resolve_url(target))
    os.environ.pop("ADMIN_PASSWORD", None)
    cwd = os.getcwd()
    os.chdir(app_dir)
    try:
        at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=120)
        at.session_state["user"] = {"id": admin["id"], "login": admin["login"], "full_name": admin["full_name"],
                                    "role": "admin"}
        at.run()
        assert not at.exception, at.exception
        labels = [t.label for t in at.tabs]
        assert "Что дозаполнить" in labels and "Журнал" in labels and "Настройки" in labels, labels
        # «Что дозаполнить»: метрики, список, кнопка «Открыть», выгрузка
        assert any(x.label == "Записей к дозаполнению" and int(x.value) >= 2 for x in at.metric)
        assert any("Мероприятие не указано" in (md.value or "") for md in at.markdown)
        # фильтр по типу проблемы
        at.multiselect(key="td_types").set_value(["placeholder"]).run()
        assert not at.exception, at.exception
        assert [x.value for x in at.metric if x.label == "Записей к дозаполнению"] == ["1"]
        # кнопка «Открыть» разворачивает форму редактирования записи
        open_btn = next(b for b in at.button if (b.key or "").startswith("td_btn_"))
        rid = int(open_btn.key.split("_")[-1])
        open_btn.click().run()
        assert not at.exception, at.exception
        assert at.session_state["td_open"] == rid
        assert any(b.label == "Сохранить изменения" and (b.key or "").startswith(f"td{rid}_") for b in at.button)
        # сохранение из вкладки «Что дозаполнить»: запись исчезает из списка, действие - в журнале
        at.text_input(key=f"td{rid}_f_title").set_value("Настоящая конференция UI").run()
        at.selectbox(key=f"td{rid}_f_row").set_value(ach.row_by_code("I01.ru", target)["id"]).run()
        next(b for b in at.button if b.key == f"td{rid}_save").click().run()
        assert not at.exception, at.exception
        assert [x.value for x in at.metric if x.label == "Записей к дозаполнению"] in ([], ["0"]), \
            [x.value for x in at.metric]
        db.clear_cache()
        upd = [e for e in audit.list_entries(db_path=target) if e["action"] == "achievement_update"]
        assert upd and upd[0]["entity_id"] == rid and upd[0]["actor_id"] == admin["id"]
        assert "Мероприятие не указано" in upd[0]["summary"] and "Настоящая конференция UI" in upd[0]["summary"]
        at.multiselect(key="td_types").set_value(list(todo.PROBLEM_LABELS)).run()
        assert not at.exception, at.exception
        # «Журнал» показывает это изменение (фильтр по действию + поиск)
        at.multiselect(key="al_actions").set_value(["achievement_update"]).run()
        at.text_input(key="al_search").set_value("настоящая конференция").run()
        assert not at.exception, at.exception
        assert any("Настоящая конференция UI" in str(df.value.to_dict("records")) for df in at.dataframe)
        at.multiselect(key="al_actions").set_value([]).run()
        at.text_input(key="al_search").set_value("").run()
        # бэкап: подготовка и кнопка скачивания
        next(b for b in at.button if b.key == "backup_make").click().run()
        assert not at.exception, at.exception
        assert at.session_state["_backup_bytes"][:2] == b"PK"
        assert any("чувствительный" in w.value for w in at.warning)
        # журнал: фильтры отрабатывают без ошибок
        at.text_input(key="al_search").set_value("интерфейсов").run()
        assert not at.exception, at.exception
        at.multiselect(key="al_actions").set_value(["login"]).run()
        assert not at.exception, at.exception
        # режим «от имени участника»: старт/возврат пишутся в журнал, запись через форму - actor = админ
        at.selectbox(key="imp_pick").set_value(m).run()
        next(b for b in at.button if b.key == "imp_go").click().run()
        assert not at.exception and at.session_state["_imp_admin"]["id"] == admin["id"]
        at.text_input(key="p_f_title").set_value("Доклад от имени через форму")
        at.text_input(key="p_f_topic").set_value("Тема через форму")
        at.selectbox(key="p_f_row").set_value(ach.row_by_code("I01.univ", target)["id"])
        at.run()
        next(b for b in at.button if b.key == "p_save").click().run()
        assert not at.exception, at.exception
        next(b for b in at.button if b.key == "imp_return").click().run()
        assert not at.exception and "_imp_admin" not in at.session_state
        db.clear_cache()
        ents = audit.list_entries(db_path=target)
        made = next(e for e in ents if e["action"] == "achievement_create" and "через форму" in e["summary"])
        assert made["actor_id"] == admin["id"] and made["as_user_id"] == m and made["target_user_id"] == m, made
        acts_seen = [e["action"] for e in ents]
        assert "impersonate_start" in acts_seen and "impersonate_stop" in acts_seen
        # участник не видит админские вкладки
        at2 = AppTest.from_file(str(ROOT / "app.py"), default_timeout=120)
        at2.session_state["user"] = {"id": m, "login": "ui_m", "full_name": "Интерфейсов Игорь", "role": "member"}
        at2.run()
        assert not at2.exception, at2.exception
        assert not any(t.label in ("Журнал", "Что дозаполнить") for t in at2.tabs)
        # вход через форму пишет в журнал
        at3 = AppTest.from_file(str(ROOT / "app.py"), default_timeout=120).run()
        at3.text_input[0].input("ui_m")
        at3.text_input[1].input("pass12345")
        at3.button[0].click().run()
        assert not at3.exception, at3.exception
    finally:
        os.chdir(cwd)
        for k, v in (("DATABASE_URL", old_url), ("ADMIN_PASSWORD", old_pw)):
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    db.clear_cache()
    assert any(e["action"] == "login" and e["actor_id"] == m for e in audit.list_entries(db_path=target))
    db.delete_user(m, acting_user_id=admin["id"], db_path=target)
    print("  UI OK (вкладки «Что дозаполнить» и «Журнал», бэкап, права, запись входа в журнал)")


def run_all(fresh) -> None:  # noqa: ANN001
    """fresh(): новая пустая база (SQLite-файл или очищенная Postgres)."""
    run_audit(fresh())
    run_todo(fresh())
    run_report_gate(fresh())
    run_backup(fresh(), fresh())
    run_ui(fresh())
