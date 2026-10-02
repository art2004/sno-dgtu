"""Smoke tests: поиск по своим работам, возможные дубли мероприятий, отчёт «Не заходили и ничего не вносили».
Вызываются из smoke_test.py (SQLite по умолчанию, Postgres через SMOKE_DATABASE_URL)."""

from __future__ import annotations

import os
from datetime import date
from pathlib import Path

from sqlalchemy import text

import achievements as ach
import audit
import db

ROOT = Path(__file__).resolve().parent
_N = iter(range(100, 99999))

CONF = "Международная научно-практическая конференция «Инновации в науке»"


def _admin(target) -> dict:  # noqa: ANN001
    return next(u for u in db.list_users(db_path=target) if u["role"] == "admin")


def _doklad(target, owner, title, when, topic="Тема доклада", number=True, date_to=None,  # noqa: ANN001
            members=None) -> int:
    K = ach.kind_by_code("doklad", target)
    data = {"kind_id": K["id"], "owner_id": owner, "row_id": ach.row_by_code("I01.reg", target)["id"],
            "title": title, "date_from": when, "date_to": date_to, "topic": topic, "ochno": True,
            "number": f"Р-Н-{next(_N)}-26" if number is True else number, "members": members or []}
    return ach.save_achievement(data, _admin(target), db_path=target)["id"]


def _events(target) -> list[dict]:  # noqa: ANN001
    db.clear_cache()
    with db.get_engine(target).connect() as conn:
        return [dict(r._mapping) for r in conn.execute(text("SELECT * FROM events ORDER BY id"))]


def _ach_rows(target) -> dict[int, dict]:  # noqa: ANN001
    db.clear_cache()
    with db.get_engine(target).connect() as conn:
        return {r._mapping["id"]: dict(r._mapping) for r in conn.execute(text("SELECT * FROM achievements"))}


def _build(target) -> dict:  # noqa: ANN001
    """Тестовые данные: три записи об одной конференции (полное, неполное название, другая дата),
    плюс «чужие» мероприятия, которые дублями считаться не должны."""
    db.init_db(db_path=target, seed_admin=True)
    u = {n: db.create_user(n, "pass12345", f"Участник {n}", db_path=target) for n in ("u1", "u2", "u3", "u4")}
    a = _doklad(target, u["u1"], CONF, "2026-03-10", "Нейросети", number="Р-Н-1-26")
    b = _doklad(target, u["u2"], "Инновации в науке", "2026-03-10", "Ёлочные игрушки", number="Р-Н-2-26")
    c = _doklad(target, u["u3"], "международная научно практическая конференция Инновации в науке!",
                "2026-03-12", "Графы", number="Р-Н-3-26")
    other = {
        "year": _doklad(target, u["u1"], CONF, "2025-03-10", "Прошлый год", number="Р-Н-4-25"),
        "far": _doklad(target, u["u1"], CONF, "2026-04-20", "Через полтора месяца", number="Р-Н-5-26"),
        "diff": _doklad(target, u["u2"], "Весенняя олимпиада по математике", "2026-03-10", number="Р-Н-6-26"),
        "n15": _doklad(target, u["u2"], "Форум «Наука» XV", "2026-05-05", number="Р-Н-7-26"),
        "n16": _doklad(target, u["u3"], "Форум «Наука» XVI", "2026-05-05", number="Р-Н-8-26"),
    }
    K = ach.kind_by_code("contest", target)
    other["contest"] = ach.save_achievement(
        {"kind_id": K["id"], "owner_id": u["u3"], "row_id": ach.row_by_code("I03.ru", target)["id"],
         "title": CONF, "date_from": "2026-03-10", "number": "Р-Н-9-26"}, _admin(target), db_path=target)["id"]
    return {"u": u, "a": a, "b": b, "c": c, **other}


def run_title_rules() -> None:
    assert db.title_key("«Ёлка»,  Новая-Год!") == "елка новая год"
    m = db.title_match
    assert m(CONF, "Инновации в науке")["kind"] == "contains"
    assert m("Инновации в науке", "ИННОВАЦИИ   В НАУКЕ")["kind"] == "same"
    assert m("Конференция Инновации в науке", "Конференция «Инновации в науки»")["kind"] in ("fuzzy", "contains")
    assert m("Форум «Наука» XV", "Форум «Наука» XVI") is None            # номера разные
    assert m("Конференция 2025", "Конференция 2026") is None
    assert m("Олимпиада по математике", "Олимпиада по физике")["kind"] == "loose"   # годно только при общей дате
    kind = lambda a, b: (m(a, b) or {}).get("kind")  # noqa: E731
    assert kind("Конференция", "Международная конференция") != "contains"                       # одно общее слово
    assert kind("Научная конференция", "Международная научная конференция по химии") != "contains"  # общие слова
    ev = lambda t, d, ty="конференция": {"title": t, "type": ty, "event_date": d}  # noqa: E731
    assert db.similar_events(ev(CONF, "2026-03-10"), ev("Инновации в науке", "2026-03-12"), 3)
    assert not db.similar_events(ev(CONF, "2026-03-10"), ev("Инновации в науке", "2026-03-20"), 3)
    assert not db.similar_events(ev(CONF, "2026-12-30"), ev("Инновации в науке", "2027-01-01"), 3)   # разные годы
    assert not db.similar_events(ev(CONF, "2026-03-10"), ev("Инновации в науке", "2026-03-10", "конкурс"), 3)
    assert db.similar_events(ev("Олимпиада по математике", "2026-03-10"), ev("Олимпиада по физике", "2026-03-10"), 3)
    assert not db.similar_events(ev("Олимпиада по математике", "2026-03-10"), ev("Олимпиада по физике", "2026-03-11"), 3)
    print("  правила сходства названий OK (регистр/ё/кавычки, неполное название, номера и годы, тип, окно дат)")


def run_search(target) -> None:  # noqa: ANN001
    ids = _build(target)
    u1 = ids["u"]["u1"]
    u2 = ids["u"]["u2"]
    db.clear_cache()
    recs = ach.list_achievements(owner_id=u2, with_coauthored=True, db_path=target)
    assert len(recs) == 3, [r["title"] for r in recs]
    f = lambda q: sorted(r["title"] for r in ach.search_records(recs, q))  # noqa: E731
    assert f("") == f("   ") == sorted(r["title"] for r in recs)
    assert f("р-н-2-26") == ["Инновации в науке"] and f("Р-Н-2-26") == f("рн226")
    assert f("№ Р-Н-2-26") == ["Инновации в науке"]
    assert f("2-26") == ["Инновации в науке"]                       # часть номера
    assert f("ИННОВАЦИИ") == f("инновации в") == ["Инновации в науке"]
    assert f("елочные") == f("ёлочные") == ["Инновации в науке"]      # ё = е, по теме доклада
    assert f("олимпиада матем") == ["Весенняя олимпиада по математике"]   # части слов, разные слова
    assert f("олимпиада 6-26") == ["Весенняя олимпиада по математике"]    # слово + номер
    assert f("форум") == ["Форум «Наука» XV"]
    assert f("нет такого") == [] and f("Р-Н-999-99") == []
    # не видит чужого: у u1 нет записи u2
    mine1 = ach.list_achievements(owner_id=u1, with_coauthored=True, db_path=target)
    assert all(r["owner_id"] == u1 or r["role"] == "coauthor" for r in mine1)
    assert ach.search_records(mine1, "Р-Н-2-26") == []
    print("  поиск по своим работам OK (номер целиком/частью/без дефисов, название, тема, ё=е, регистр, пусто)")


def run_dups(target) -> None:  # noqa: ANN001
    ids = _build(target)
    u = ids["u"]
    # --- подсказка при внесении
    ev_a = ach.get_achievement(ids["a"], target)["event_id"]
    ev_b = ach.get_achievement(ids["b"], target)["event_id"]
    ev_c = ach.get_achievement(ids["c"], target)["event_id"]
    assert len({ev_a, ev_b, ev_c}) == 3                             # три разных мероприятия: в этом и проблема
    hint = db.find_similar_events("Инновации в науке (конференция)", "конференция", "2026-03-11", db_path=target)
    got = {h["event_id"] for h in hint}
    assert {ev_a, ev_b, ev_c} <= got, hint
    assert all(h["event_date"][:4] == "2026" for h in hint)
    assert not db.find_similar_events("Совсем другое", "конференция", "2026-03-11", db_path=target)
    assert not db.find_similar_events("Инновации в науке", "конференция", "2026-03-10", exclude_event_id=ev_b,
                                      db_path=target) or all(h["event_id"] != ev_b for h in
                                      db.find_similar_events("Инновации в науке", "конференция", "2026-03-10",
                                                             exclude_event_id=ev_b, db_path=target))
    # точное совпадение подсказкой не считается (его и так подхватывает общее мероприятие)
    assert all(h["event_id"] != ev_b for h in db.find_similar_events("инновации   в НАУКЕ", "конференция",
                                                                       "2026-03-10", db_path=target))
    # --- группы для админа
    groups = db.list_duplicate_groups(3, db_path=target)
    assert len(groups) == 1, [[e["title"] for e in g["events"]] for g in groups]
    g = groups[0]
    assert {e["id"] for e in g["events"]} == {ev_a, ev_b, ev_c}
    assert {r["owner"] for e in g["events"] for r in e["records"]} == {"Участник u1", "Участник u2", "Участник u3"}
    assert g["reasons"]
    g0 = db.list_duplicate_groups(0, db_path=target)                  # только одна и та же дата: a + b
    assert len(g0) == 1 and {e["id"] for e in g0[0]["events"]} == {ev_a, ev_b}
    assert not db.list_duplicate_groups(0, db_path=target)[0]["events"][0]["type"] != "конференция"
    # другой год, далёкая дата, другой тип, другой номер форума в группу не попали
    in_group = {e["id"] for e in g["events"]}
    for k in ("year", "far", "diff", "n15", "n16", "contest"):
        assert ach.get_achievement(ids[k], target)["event_id"] not in in_group, k

    # --- объединение: до / после
    before_a = _ach_rows(target)
    before_events = len(_events(target))
    keep = ("owner_id", "number", "topic", "kind_id", "indicator_id", "row_id", "ochno", "owner_share", "externals")
    snap_before = {i: {k: r[k] for k in keep} for i, r in before_a.items()}
    admin = _admin(target)
    ctx = {"actor": admin, "as_user": None}
    # mixed types / single id → отказ, ничего не меняется
    for bad in ([ev_a], [ev_a, ach.get_achievement(ids["contest"], target)["event_id"]]):
        try:
            ach.merge_event_group(bad, CONF, "2026-03-10", db_path=target, audit_ctx=ctx)
            raise AssertionError("ожидали ValueError")
        except ValueError:
            pass
    assert len(_events(target)) == before_events and _ach_rows(target) == before_a
    try:
        ach.merge_event_group([ev_a, ev_b, ev_c], "  ", "2026-03-10", db_path=target, audit_ctx=ctx)
        raise AssertionError("пустое название должно отклоняться")
    except ValueError:
        pass
    res = ach.merge_event_group([ev_a, ev_b, ev_c], CONF, "2026-03-10", db_path=target, audit_ctx=ctx)
    assert res["event_id"] in (ev_a, ev_b, ev_c) and len(res["removed"]) == 2
    evs = _events(target)
    assert len(evs) == before_events - 2
    after_a = _ach_rows(target)
    assert set(after_a) == set(before_a)                                  # ни одна запись не потеряна
    assert {i: {k: r[k] for k in keep} for i, r in after_a.items()} == snap_before   # номера, темы, доли - как были
    for aid in (ids["a"], ids["b"], ids["c"]):
        r = after_a[aid]
        assert r["event_id"] == res["event_id"] and r["title"] == CONF and r["date_from"] == "2026-03-10", r
    assert not db.list_duplicate_groups(3, db_path=target)
    # событие по записи теперь «то же самое»: сохранение без изменений не создаёт новых мероприятий
    rec = ach.get_achievement(ids["b"], target)
    ach.save_achievement({"kind_id": rec["kind_id"], "owner_id": rec["owner_id"], "row_id": rec["row_id"],
                          "title": rec["title"], "date_from": rec["date_from"], "topic": rec["topic"],
                          "ochno": True, "number": rec["number"]}, admin, achievement_id=ids["b"], db_path=target)
    assert len(_events(target)) == before_events - 2
    assert ach.get_achievement(ids["b"], target)["event_id"] == res["event_id"]
    db.clear_cache()
    log = [e for e in audit.list_entries(db_path=target) if e["action"] == "events_merge"]
    assert len(log) == 1 and log[0]["entity_id"] == res["event_id"] and "Объединено мероприятий: 3" in log[0]["summary"]
    assert log[0]["actor_id"] == admin["id"] and log[0]["details"]["after"]["title"] == CONF
    print("  дубли: подсказка, группы (неполное название, другая дата, не склеились год/тип/номер), "
          "объединение (записи, номера, темы на месте; журнал)")


def run_merge_custom(target) -> None:  # noqa: ANN001
    """Свои название и дата; старые участия (participations), общий участник, заседание; цель вне выбранных."""
    db.init_db(db_path=target, seed_admin=True)
    u1 = db.create_user("m1", "pass12345", "Мержов Один", db_path=target)
    u2 = db.create_user("m2", "pass12345", "Мержов Два", db_path=target)
    admin = _admin(target)
    ctx = {"actor": admin, "as_user": None}
    # старые участия: u2 внёс два варианта, у второго номера нет
    db.add_participation_ex(u1, "Весенняя школа ИТ", "конференция", "2026-04-02", achievement_number="Р-Н-10-26",
                            db_path=target)
    db.add_participation_ex(u2, "Весенняя школа ИТ (ДГТУ)", "конференция", "2026-04-03",
                            achievement_number="Р-Н-11-26", db_path=target)
    db.add_participation_ex(u2, "весенняя школа ит", "конференция", "2026-04-01", db_path=target)
    db.init_db(db_path=target)                       # перенос участий в достижения
    evs = {e["title"]: e["id"] for e in _events(target)}
    assert len(evs) == 3
    mid = db.add_meeting(date(2026, 4, 3), "13:30", "ауд. 1", "Школа", "Собрание", "Конференция",
                         event_id=evs["Весенняя школа ИТ (ДГТУ)"], db_path=target)
    groups = db.list_duplicate_groups(3, db_path=target)
    assert len(groups) == 1 and len(groups[0]["events"]) == 3
    a_before = _ach_rows(target)
    with db.get_engine(target).connect() as conn:
        p_before = conn.execute(text("SELECT COUNT(*) FROM participations")).scalar_one()
    res = ach.merge_event_group(list(evs.values()), "Весенняя школа информационных технологий", "2026-04-02",
                                db_path=target, audit_ctx=ctx)
    assert res["participations_merged"] == 1                              # у u2 два «участия» в одном мероприятии
    a_after = _ach_rows(target)
    assert set(a_after) == set(a_before) and len(a_after) == 3            # все три достижения живы
    assert {r["number"] for r in a_after.values()} == {r["number"] for r in a_before.values()}
    assert {r["title"] for r in a_after.values()} == {"Весенняя школа информационных технологий"}
    assert {r["date_from"] for r in a_after.values()} == {"2026-04-02"}
    with db.get_engine(target).connect() as conn:
        p_after = conn.execute(text("SELECT COUNT(*) FROM participations")).scalar_one()
        nums = {r[0]: r[1] for r in conn.execute(text("SELECT user_id, achievement_number FROM participations"))}
    assert p_after == p_before - 1 and nums[u2] == "Р-Н-11-26" and nums[u1] == "Р-Н-10-26"   # номер не потерян
    assert len(_events(target)) == 1
    assert db.get_meeting(mid, target)["event_id"] == res["event_id"]            # заседание не потеряло связь
    # цель вне выбранных: новое «неполное» мероприятие склеивается с уже существующим каноническим
    K = ach.kind_by_code("doklad", target)
    row = ach.row_by_code("I01.reg", target)["id"]
    extra = ach.save_achievement({"kind_id": K["id"], "owner_id": u1, "row_id": row, "title": "Весенняя школа ИТ",
                                  "date_from": "2026-04-05", "topic": "Ещё доклад", "ochno": True,
                                  "number": "Р-Н-12-26"}, admin, db_path=target)["id"]
    ev_extra = ach.get_achievement(extra, target)["event_id"]
    main = _events(target)[0]["id"]
    assert ev_extra != main
    r2 = ach.merge_event_group([ev_extra, main], "Весенняя школа информационных технологий", "2026-04-02",
                               db_path=target, audit_ctx=ctx)
    assert r2["event_id"] == main and r2["removed"] == [ev_extra]
    assert ach.get_achievement(extra, target)["event_id"] == main
    assert ach.get_achievement(extra, target)["date_from"] == "2026-04-02"
    assert len(_events(target)) == 1
    # конкурс с датой окончания и участниками: дата окончания сдвигается вместе с началом, участники на месте
    KC = ach.kind_by_code("contest", target)
    rc = ach.row_by_code("I03.ru", target)["id"]

    def contest(owner, title, d1, d2, members, number):  # noqa: ANN001, ANN202
        return ach.save_achievement({"kind_id": KC["id"], "owner_id": owner, "row_id": rc, "title": title,
                                     "date_from": d1, "date_to": d2, "members": members, "number": number},
                                    admin, db_path=target)["id"]
    c1 = contest(u1, "Кейс-чемпионат Дон", "2026-05-10", "2026-05-12", [u2], "Р-Н-21-26")
    c2 = contest(u2, "Кейс чемпионат «Дон»", "2026-05-12", "2026-05-14", [u1], "Р-Н-22-26")
    e1, e2 = (ach.get_achievement(i, target)["event_id"] for i in (c1, c2))
    assert e1 != e2 and len(db.list_duplicate_groups(3, db_path=target)) == 1
    ach.merge_event_group([e1, e2], "Кейс-чемпионат Дон", "2026-05-10", db_path=target, audit_ctx=ctx)
    r1, r2 = ach.get_achievement(c1, target), ach.get_achievement(c2, target)
    assert (r1["date_from"], r1["date_to"]) == ("2026-05-10", "2026-05-12")
    assert (r2["date_from"], r2["date_to"]) == ("2026-05-10", "2026-05-12")        # +2 дня сдвинуты назад
    assert [p["user_id"] for p in r1["people"]] == [u2] and [p["user_id"] for p in r2["people"]] == [u1]
    assert r1["event_id"] == r2["event_id"] and r1["number"] == "Р-Н-21-26" and r2["number"] == "Р-Н-22-26"
    print("  объединение со своим названием и датой OK (старые участия, общий участник, номер перенесён, цель вне выбора)")


def run_not_dup(target) -> None:  # noqa: ANN001
    ids = _build(target)
    admin = _admin(target)
    ctx = {"actor": admin, "as_user": None}
    groups = db.list_duplicate_groups(3, db_path=target)
    assert len(groups) == 1
    evs = [e["id"] for e in groups[0]["events"]]
    n = ach.mark_events_not_dup(evs, admin, db_path=target, audit_ctx=ctx)
    assert n == 3
    assert ach.mark_events_not_dup(evs, admin, db_path=target, audit_ctx=ctx) == 0       # идемпотентно
    assert not db.list_duplicate_groups(3, db_path=target)
    assert len(db.list_duplicate_groups(3, db_path=target, include_hidden=True)) == 1
    assert len(db.list_not_duplicates(target)) == 3
    # новое похожее мероприятие снова даёт группу, но скрытые пары остаются скрытыми
    new = _doklad(target, ids["u"]["u4"], "Инновации в науке и технике", "2026-03-11", number="Р-Н-20-26")
    ev_new = ach.get_achievement(new, target)["event_id"]
    g = db.list_duplicate_groups(3, db_path=target)
    assert len(g) == 1 and ev_new in {e["id"] for e in g[0]["events"]}
    assert not any(_ == (min(x, y), max(x, y)) for x in evs for y in evs if x != y
                   for _ in [(p[0], p[1]) for p in g[0]["pairs"]])
    # вернуть пару
    pid = db.list_not_duplicates(target)[0]["id"]
    assert ach.unmark_event_not_dup(pid, admin, db_path=target, audit_ctx=ctx)
    assert len(db.list_not_duplicates(target)) == 2
    db.clear_cache()
    acts = [e["action"] for e in audit.list_entries(db_path=target)]
    assert acts.count("events_not_dup") == 2 and acts.count("events_not_dup_undo") == 1
    # удаление мероприятия убирает его пары (ON DELETE CASCADE / ручная чистка при объединении)
    ev_x = evs[0]
    res = ach.merge_event_group([ev_x, evs[1]], "Инновации в науке (общее)", "2026-03-10", db_path=target, audit_ctx=ctx)
    with db.get_engine(target).connect() as conn:
        left = conn.execute(text("SELECT COUNT(*) FROM event_not_dup WHERE event_a = :s OR event_b = :s"),
                            {"s": res["removed"][0]}).scalar_one()
    assert left == 0
    print("  «Не дубль» OK (скрывает группу, идемпотентно, новые мероприятия видны, отмена, журнал, чистка пар)")


def run_ui(target) -> None:  # noqa: ANN001
    from streamlit.testing.v1 import AppTest

    ids = _build(target)
    u = ids["u"]
    admin = _admin(target)
    old_url, old_pw = os.environ.get("DATABASE_URL"), os.environ.get("ADMIN_PASSWORD")
    os.environ["DATABASE_URL"] = str(db.resolve_url(target))
    os.environ.pop("ADMIN_PASSWORD", None)
    cwd = os.getcwd()
    os.chdir(str(ROOT))

    def session(user: dict) -> AppTest:
        at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=120)
        at.session_state["user"] = user
        at.run()
        assert not at.exception, at.exception
        return at

    def pick(at, label):  # noqa: ANN001, ANN202
        return next(b for b in at.button if b.label == label)

    try:
        # ── участник: поиск
        m2 = {"id": u["u2"], "login": "u2", "full_name": "Участник u2", "role": "member"}
        at = session(m2)
        assert at.text_input(key="mine_q")
        assert not [c for c in at.caption if (c.value or "").startswith("Найдено")]
        at.text_input(key="mine_q").set_value("Р-Н-2-26").run()
        assert not at.exception, at.exception
        assert [c.value for c in at.caption if (c.value or "").startswith("Найдено")] == ["Найдено 1 из 3"]
        shown = " ".join(md.value for md in at.markdown)
        assert "Инновации в науке" in shown and "Весенняя олимпиада" not in shown
        at.text_input(key="mine_q").set_value("ЁЛОЧНЫЕ игрушки").run()
        assert [c.value for c in at.caption if (c.value or "").startswith("Найдено")] == ["Найдено 1 из 3"]
        at.text_input(key="mine_q").set_value("zzz-нет").run()
        assert not at.exception, at.exception
        assert [c.value for c in at.caption if (c.value or "").startswith("Найдено")] == ["Найдено 0 из 3"]
        assert any("ничего не найдено" in i.value for i in at.info)
        at.text_input(key="mine_q").set_value("").run()
        assert not [c for c in at.caption if (c.value or "").startswith("Найдено")]
        # ── участник: подсказка «Похоже на уже внесённое»
        m4 = {"id": u["u4"], "login": "u4", "full_name": "Участник u4", "role": "member"}
        at = session(m4)
        kid = ach.kind_by_code("doklad", target)["id"]
        at.selectbox(key="p_kind").set_value(kid).run()
        at.text_input(key="p_f_title").set_value("Инновации в науке").run()
        at.date_input(key="p_f_date_from").set_value(date(2026, 3, 11)).run()
        assert not at.exception, at.exception
        assert any("Похоже на уже внесённое" in w.value for w in at.warning), [w.value for w in at.warning]
        pick_btns = [b for b in at.button if (b.key or "").startswith("p_pick_ev_")]
        assert len(pick_btns) == 3, [b.key for b in pick_btns]
        txt = " ".join(md.value for md in at.markdown)
        assert "Участник u1" not in txt and "Р-Н-1-26" not in txt                   # участник не видит, кто внёс
        pick_btns[0].click().run()
        assert not at.exception, at.exception
        assert at.session_state["p_f_title"] in (CONF, "Инновации в науке", ach.get_achievement(ids["c"], target)["title"])
        assert at.session_state["p_f_date_from"] in (date(2026, 3, 10), date(2026, 3, 12))
        # ── админ: вкладка «Дубли мероприятий»
        at = session(admin)
        assert "Дубли мероприятий" in [t.label for t in at.tabs]
        assert any(x.value == "Возможные дубли мероприятий" for x in at.subheader)
        assert [c.value for c in at.caption if (c.value or "").startswith("Найдено групп")] == ["Найдено групп: 1"]
        txt = " ".join(md.value for md in at.markdown)
        assert "Инновации в науке" in txt and "Участник u1" in txt and "Р-Н-1-26" in txt
        before = _ach_rows(target)
        n_events = len(_events(target))
        # слайдер дат: 0 дней - остаётся пара с одной датой
        at.slider(key="evd_days").set_value(0).run()
        assert [c.value for c in at.caption if (c.value or "").startswith("Найдено групп")] == ["Найдено групп: 1"]
        at.slider(key="evd_days").set_value(3).run()
        gkey = next(b.key for b in at.button if (b.key or "").endswith("_merge")).rsplit("_", 1)[0]
        # объединить: сначала подтверждение
        next(b for b in at.button if b.key == f"{gkey}_merge").click().run()
        assert any("Объединить 3 мероприятия" in w.value for w in at.warning), [w.value for w in at.warning]
        assert len(_events(target)) == n_events
        next(b for b in at.button if b.key == f"{gkey}_cancel").click().run()
        assert len(_events(target)) == n_events
        # своё название и дата
        at.radio(key=f"{gkey}_pick").set_value(0).run()
        at.text_input(key=f"{gkey}_title").set_value("Научная конференция «Инновации в науке» ДГТУ").run()
        at.date_input(key=f"{gkey}_date").set_value(date(2026, 3, 11)).run()
        next(b for b in at.button if b.key == f"{gkey}_merge").click().run()
        next(b for b in at.button if b.key == f"{gkey}_yes").click().run()
        assert not at.exception, at.exception
        assert any("Объединено: 3 мероприятия в одно" in s.value for s in at.success), [s.value for s in at.success]
        assert len(_events(target)) == n_events - 2
        after = _ach_rows(target)
        assert set(after) == set(before)
        assert {r["title"] for i, r in after.items() if i in (ids["a"], ids["b"], ids["c"])} == \
            {"Научная конференция «Инновации в науке» ДГТУ"}
        assert {r["date_from"] for i, r in after.items() if i in (ids["a"], ids["b"], ids["c"])} == {"2026-03-11"}
        assert [c.value for c in at.caption if (c.value or "").startswith("Найдено групп")] == ["Найдено групп: 0"]
        db.clear_cache()
        assert any(e["action"] == "events_merge" and e["actor_id"] == admin["id"] for e in audit.list_entries(db_path=target))
        # «Не дубль» через интерфейс: новое похожее мероприятие, потом скрываем
        ua_id = _doklad(target, u["u4"], "Инновации в науке ДГТУ", "2026-03-12", number="Р-Н-30-26")
        assert ua_id
        db.clear_cache()
        at = session(admin)
        assert [c.value for c in at.caption if (c.value or "").startswith("Найдено групп")] == ["Найдено групп: 1"]
        gkey = next(b.key for b in at.button if (b.key or "").endswith("_nodup")).rsplit("_", 1)[0]
        next(b for b in at.button if b.key == f"{gkey}_nodup").click().run()
        assert not at.exception, at.exception
        assert [c.value for c in at.caption if (c.value or "").startswith("Найдено групп")] == ["Найдено групп: 0"]
        assert any("Скрытые пары" in e.label for e in at.expander)
        db.clear_cache()
        assert any(e["action"] == "events_not_dup" for e in audit.list_entries(db_path=target))
        # вернуть
        next(b for b in at.button if (b.key or "").startswith("evd_unhide_")).click().run()
        assert not at.exception, at.exception
        assert [c.value for c in at.caption if (c.value or "").startswith("Найдено групп")] == ["Найдено групп: 1"]
        # участник не видит вкладку
        at2 = session({"id": u["u1"], "login": "u1", "full_name": "Участник u1", "role": "member"})
        assert "Дубли мероприятий" not in [t.label for t in at2.tabs]
    finally:
        os.chdir(cwd)
        for k, v in (("DATABASE_URL", old_url), ("ADMIN_PASSWORD", old_pw)):
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    print("  UI OK (поиск у участника, подсказка и «Выбрать это», вкладка админа: объединение с подтверждением, "
          "своё название, «Не дубль» и возврат)")


def run_inactive(target) -> None:  # noqa: ANN001
    import io

    import openpyxl
    from streamlit.testing.v1 import AppTest

    import inactive

    db.init_db(db_path=target, seed_admin=True)
    admin = _admin(target)
    mk = lambda login, name: db.create_user(login, "pass12345", name, db_path=target)  # noqa: E731
    a = mk("ia", "Неактивный Антон")                 # не входил, ничего не внёс
    b = mk("ib", "Ёлкин Борис")                      # входил, ничего не внёс
    c = mk("ic", "Внесов Виктор")                    # не входил, но запись есть (внёс админ)
    d = mk("id", "Отключённый Дмитрий")              # не входил, ничего, но отключён
    h = mk("ih", "Журнальный Игорь")                 # входил до появления last_login: есть только запись в журнале
    g = mk("ig", "Старов Глеб")                      # только старое участие (до достижений)
    db.update_user(d, active=False, acting_user_id=admin["id"], db_path=target)
    _doklad(target, c, "Конференция для неактивных", "2026-02-02")
    db.add_participation_ex(g, "Старая конференция", "конференция", "2026-01-15", db_path=target)
    db.init_db(db_path=target)
    assert db.touch_last_login(b, db_path=target)
    audit.log("login", actor={"id": h, "login": "ih", "full_name": "Журнальный Игорь"}, entity="user", entity_id=h,
              db_path=target)
    db.clear_cache()
    # колонка last_login есть, у старых пользователей пустая
    assert "last_login" in db.list_users(db_path=target)[0]
    by = {r["login"]: r for r in db.list_user_activity(target)}
    assert by["ia"]["last_login"] is None and by["ia"]["records"] == 0
    assert by["ib"]["last_login"] and by["ib"]["records"] == 0
    assert by["ic"]["last_login"] is None and by["ic"]["records"] == 1
    assert by["ih"]["last_login"] and by["ig"]["records"] == 1             # перенесённое участие считается один раз
    names = lambda **kw: sorted(r["login"] for r in inactive.list_inactive(db_path=target, **kw))  # noqa: E731
    assert names() == ["ia"]                                              # по умолчанию: не заходили и ничего не внесли
    assert not by["id"]["active"] and names(hide_disabled=False) == ["ia", "id"]
    assert "admin" in names(hide_admins=False)
    assert sorted(r["login"] for r in inactive.list_inactive("no_login", db_path=target)) == ["ia", "ic", "ig"]
    assert sorted(r["login"] for r in inactive.list_inactive("no_records", db_path=target)) == ["ia", "ib", "ih"]
    # отметка входа по cookie не чаще раза в интервал
    assert not db.touch_last_login(b, min_gap_seconds=3600, db_path=target)
    with db.get_engine(target).begin() as conn:
        conn.execute(text("UPDATE users SET last_login = '2020-01-01T10:00:00' WHERE id = :i"), {"i": b})
    assert db.touch_last_login(b, min_gap_seconds=3600, db_path=target)
    # выгрузки
    recs = inactive.list_inactive("no_login", db_path=target)
    ws = openpyxl.load_workbook(io.BytesIO(inactive.build_xlsx(recs, inactive.MODES["no_login"]))).active
    rows = list(ws.iter_rows(values_only=True))
    assert list(rows[0]) == inactive.COLUMNS and len(rows) == 1 + len(recs) and rows[1][1] in ("ia", "ic", "ig")
    csv = inactive.build_csv(recs)
    assert csv.startswith("\ufeff".encode("utf-8")) and "Неактивный Антон".encode("utf-8") in csv
    # миграция: база без колонки last_login (старая версия) получает её при запуске, данные целы
    with db.get_engine(target).begin() as conn:
        conn.execute(text("ALTER TABLE users DROP COLUMN last_login"))
    db.init_db(db_path=target)
    db.clear_cache()
    assert "last_login" in db.list_users(db_path=target)[0] and len(db.list_users(db_path=target)) >= 7
    db.init_db(db_path=target)                       # повторный запуск безопасен
    assert all(r["last_login"] is None for r in db.list_users(db_path=target) if r["login"] == "ib")   # у старых пусто
    assert db.touch_last_login(b, db_path=target)
    # интерфейс: вход через форму ставит last_login, вкладка админа показывает список и выгрузки
    old_url, old_pw = os.environ.get("DATABASE_URL"), os.environ.get("ADMIN_PASSWORD")
    os.environ["DATABASE_URL"] = str(db.resolve_url(target))
    os.environ.pop("ADMIN_PASSWORD", None)
    cwd = os.getcwd()
    os.chdir(str(ROOT))
    try:
        at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=120).run()
        at.text_input[0].input("ia")
        at.text_input[1].input("pass12345")
        at.button[0].click().run()
        assert not at.exception, at.exception
        db.clear_cache()
        assert db.get_user_by_id(a, target)["last_login"]
        assert "ia" not in names()                                         # теперь заходил
        ad = AppTest.from_file(str(ROOT / "app.py"), default_timeout=120)
        ad.session_state["user"] = {"id": admin["id"], "login": admin["login"], "full_name": admin["full_name"],
                                    "role": "admin"}
        ad.run()
        assert not ad.exception, ad.exception
        assert "Не заходили и ничего не вносили" in [t.label for t in ad.tabs]
        assert any(x.value == "Не заходили и ничего не вносили" for x in ad.subheader)
        assert [x.value for x in ad.metric if x.label == "Участников в списке"] == ["0"]
        ad.radio(key="ina_mode").set_value("no_login").run()
        assert not ad.exception, ad.exception
        assert [x.value for x in ad.metric if x.label == "Участников в списке"] == ["2"]    # ic, ig (ia уже вошёл)
        df = next(x.value for x in ad.dataframe if "Последний вход (МСК)" in x.value.columns)
        assert sorted(df["Логин"]) == ["ic", "ig"] and list(df["Записей"]) == [1, 1]
        ad.radio(key="ina_mode").set_value("no_records").run()
        df = next(x.value for x in ad.dataframe if "Последний вход (МСК)" in x.value.columns)
        assert sorted(df["Логин"]) == ["ia", "ib", "ih"] and all(df["Последний вход (МСК)"])
        ad.checkbox(key="ina_adm").set_value(False).run()
        df = next(x.value for x in ad.dataframe if "Последний вход (МСК)" in x.value.columns)
        assert "admin" in list(df["Логин"])
        assert not ad.exception, ad.exception
    finally:
        os.chdir(cwd)
        for k, v in (("DATABASE_URL", old_url), ("ADMIN_PASSWORD", old_pw)):
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    print("  «Не заходили и ничего не вносили» OK (колонка last_login и миграция, вход ставит дату, журнал как запасной "
          "источник, режимы, отключённые/админы, xlsx и csv, вкладка)")


def run_all(fresh) -> None:  # noqa: ANN001
    """fresh(): новая пустая база (SQLite-файл или очищенная Postgres)."""
    run_title_rules()
    run_search(fresh())
    run_dups(fresh())
    run_merge_custom(fresh())
    run_not_dup(fresh())
    run_ui(fresh())
    run_inactive(fresh())


if __name__ == "__main__":
    import tempfile

    tmp = Path(tempfile.mkdtemp())
    counter = iter(range(1000))
    run_all(lambda: tmp / f"sno_events_{next(counter)}.db")
    print("OK")
