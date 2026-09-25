"""Smoke-test: admin seed, member create, event add, dedup, cascade, password change.

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
        conn.execute(text("DROP TABLE IF EXISTS participations, events, users CASCADE"))


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

    assert db.normalize_title("  Foo   BAR ") == "foo bar"
    assert verify_password("x", hash_password("x"))
    print(f"  events={db.count_events(db_path=target)} (expected 3), shared grant eid={eid1}")


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
        print("OK (Postgres)")
        return

    tmp = Path(tempfile.mkdtemp()) / "sno_smoke.db"
    # Without ADMIN_PASSWORD local SQLite falls back to admin123
    run(tmp, expect_admin_password=admin_pw_env or "admin123")
    print("OK")
    print(f"  smoke db: {tmp}")


if __name__ == "__main__":
    main()
