"""Smoke tests: согласие на обработку ПДн (152-ФЗ) и понятный текст о повторе номера достижения.
Вызываются из smoke_test.py (SQLite по умолчанию, Postgres через SMOKE_DATABASE_URL)."""

from __future__ import annotations

import os
from pathlib import Path

from sqlalchemy import text

import achievements as ach
import audit
import db
import privacy
import todo

ROOT = Path(__file__).resolve().parent
CONSENT_COLS = ("pd_consent_at", "pd_consent_version")


def consent_all(target) -> None:  # noqa: ANN001
    """Для UI-тестов других модулей: у всех пользователей уже есть согласие на текущую политику."""
    with db.get_engine(target).begin() as conn:
        conn.execute(text("UPDATE users SET pd_consent_at = '2026-10-08T12:00:00', pd_consent_version = :v"),
                     {"v": privacy.POLICY_VERSION})
    db.clear_cache()


def _admin(target) -> dict:  # noqa: ANN001
    return next(u for u in db.list_users(db_path=target) if u["role"] == "admin")


def _cols(target) -> set[str]:  # noqa: ANN001
    from sqlalchemy import inspect

    return {c["name"] for c in inspect(db.get_engine(target)).get_columns("users")}


class _Env:
    """DATABASE_URL на тестовую базу, без ADMIN_PASSWORD, cwd = папка приложения (как в других smoke)."""

    def __init__(self, target) -> None:  # noqa: ANN001
        self.target = target

    def __enter__(self):  # noqa: ANN204
        self.old = {k: os.environ.get(k) for k in ("DATABASE_URL", "ADMIN_PASSWORD")}
        os.environ["DATABASE_URL"] = str(db.resolve_url(self.target))
        os.environ.pop("ADMIN_PASSWORD", None)
        self.cwd = os.getcwd()
        os.chdir(str(ROOT))
        return self

    def __exit__(self, *exc) -> None:  # noqa: ANN002
        os.chdir(self.cwd)
        for k, v in self.old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _app(user: dict | None = None, **state):  # noqa: ANN003, ANN202
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=120)
    if user is not None:
        at.session_state["user"] = {k: user[k] for k in ("id", "login", "full_name", "role")}
    for k, v in state.items():
        at.session_state[k] = v
    at.run()
    assert not at.exception, at.exception
    return at


def _gated(at) -> bool:  # noqa: ANN001
    labels = [c.label for c in at.checkbox]
    return privacy.CONSENT_LABEL in labels and any(b.label == "Продолжить" for b in at.button)


def run_consent_db(target) -> None:  # noqa: ANN001
    db.init_db(db_path=target, seed_admin=True)
    admin = _admin(target)
    m = db.create_user("pd_m", "pass12345", "Согласнов Сергей", db_path=target)
    assert set(CONSENT_COLS) <= _cols(target)
    u = db.get_user_by_id(m, target)
    assert u["pd_consent_at"] is None and u["pd_consent_version"] is None
    # новый пользователь и админ - оба без согласия
    assert privacy.needs_consent(m, target) and privacy.needs_consent(admin["id"], target)
    # без галочки - отказ, в БД ничего
    ok, msg = privacy.give_consent(u, False, db_path=target)
    assert not ok and "отметьте согласие" in msg and privacy.needs_consent(m, target)
    # с галочкой - время (МСК) и версия политики в БД, запись в журнале
    ok, _ = privacy.give_consent(u, True, db_path=target)
    assert ok and not privacy.needs_consent(m, target)
    u = db.get_user_by_id(m, target)
    assert u["pd_consent_version"] == privacy.POLICY_VERSION and len(u["pd_consent_at"]) == 19, u
    assert any(r["id"] == m and r["pd_consent_at"] for r in db.list_users(db_path=target))
    db.clear_cache()
    ent = next(e for e in audit.list_entries(db_path=target) if e["action"] == "pd_consent")
    assert ent["actor_id"] == m and privacy.POLICY_VERSION in str(ent["details"])
    assert audit.action_label("pd_consent") == "Согласие на обработку ПДн"
    # новая версия политики - спросить снова; отзыв (админ) - тоже
    old = privacy.POLICY_VERSION
    try:
        privacy.POLICY_VERSION = "2099-01-01"
        assert privacy.needs_consent(m, target)
    finally:
        privacy.POLICY_VERSION = old
    assert not privacy.needs_consent(m, target)
    assert db.clear_pd_consent(m, target) and privacy.needs_consent(m, target)
    assert not db.clear_pd_consent(999999, target)
    try:
        db.set_pd_consent(999999, privacy.POLICY_VERSION, target)
        raise AssertionError("ValueError expected")
    except ValueError:
        pass
    # текст политики: заглушка оператора, нет утверждения о хранении в РФ, все реальные категории данных
    pol = privacy.policy_markdown()
    assert "[наименование оператора" in privacy.OPERATOR and privacy.OPERATOR in pol and privacy.STORAGE_PLACE in pol
    assert "на территории Российской Федерации" not in pol and "в России" not in pol
    for needle in ("логин", "роль", "хэш", "последнего входа", "соавтор", "доли участия", "журнал действий",
                   "номер достижения", "DOI", "отозвать согласие", "bcrypt", "152-ФЗ", "Роскомнадзор", "sno_auth"):
        assert needle in pol, needle

    # миграция: база старой версии (без колонок согласия) получает их при запуске, данные и входы целы
    n_users = len(db.list_users(db_path=target))
    with db.get_engine(target).begin() as conn:
        for c in CONSENT_COLS:
            conn.execute(text(f"ALTER TABLE users DROP COLUMN {c}"))
    db.clear_cache()
    assert not set(CONSENT_COLS) & _cols(target)
    db.init_db(db_path=target)
    db.init_db(db_path=target)  # повторный запуск безопасен
    db.clear_cache()
    assert set(CONSENT_COLS) <= _cols(target) and len(db.list_users(db_path=target)) == n_users
    from auth import verify_password

    assert verify_password("pass12345", db.get_user_by_id(m, target)["password_hash"])  # вход не сломан
    assert privacy.needs_consent(m, target) and privacy.needs_consent(admin["id"], target)  # спросим один раз
    if db.is_postgres(target):
        # гонка двух процессов на старте: второй видит устаревший список колонок - ADD COLUMN IF NOT EXISTS
        import sqlalchemy

        real = sqlalchemy.inspect

        class _Stale:
            def __init__(self, eng) -> None:  # noqa: ANN001
                self._i = real(eng)

            def get_columns(self, table):  # noqa: ANN001, ANN202
                cols = self._i.get_columns(table)
                return [c for c in cols if c["name"] not in CONSENT_COLS] if table == "users" else cols

            def __getattr__(self, name):  # noqa: ANN001, ANN204
                return getattr(self._i, name)

        db.inspect = _Stale
        try:
            db._migrate_schema(db.get_engine(target))
        finally:
            db.inspect = real
        assert set(CONSENT_COLS) <= _cols(target)
    print(f"  ПДн: согласие в БД OK (без галочки - отказ, время+версия, журнал, новая версия/отзыв - спросить снова, "
          f"миграция идемпотентна{', ADD COLUMN IF NOT EXISTS при гонке' if db.is_postgres(target) else ''})")


def run_consent_ui(target) -> None:  # noqa: ANN001
    db.init_db(db_path=target, seed_admin=True)
    admin = _admin(target)
    m = db.create_user("pd_ui", "pass12345", "Экранов Эдуард", db_path=target)
    member = db.get_user_by_id(m, target)
    with _Env(target):
        # экран входа: политика доступна до входа
        at = _app()
        assert any(e.label.endswith(privacy.POLICY_TITLE) for e in at.expander)
        assert any("Оператор" in (md.value or "") for md in at.markdown)
        # участник без согласия: только экран согласия, данных сайта нет
        at = _app(member)
        assert _gated(at)
        assert not at.tabs and not any(t.value == "Мои достижения" for t in at.title)
        assert any(e.label.endswith(privacy.POLICY_TITLE) for e in at.expander)
        # «Продолжить» без галочки - ошибка, доступа нет, в БД пусто
        next(b for b in at.button if b.label == "Продолжить").click().run()
        assert not at.exception and _gated(at) and any("отметьте согласие" in e.value for e in at.error)
        db.clear_cache()
        assert db.get_user_by_id(m, target)["pd_consent_at"] is None
        # галочка + «Продолжить» - согласие записано, открывается кабинет
        at.checkbox(key="pd_consent_check").check()
        next(b for b in at.button if b.label == "Продолжить").click().run()
        assert not at.exception, at.exception
        assert not _gated(at) and any(t.value == "Мои достижения" for t in at.title)
        db.clear_cache()
        assert db.get_user_by_id(m, target)["pd_consent_version"] == privacy.POLICY_VERSION
        # сайдбар: ссылка на политику и дата согласия
        assert any(b.label.endswith(privacy.POLICY_TITLE) for b in at.sidebar.button)
        assert any("Согласие на обработку ПДн дано" in c.value for c in at.sidebar.caption)
        # новая сессия (обновление страницы) - больше не спрашиваем
        at = _app(member)
        assert not _gated(at) and any(t.value == "Мои достижения" for t in at.title)
        # «Выйти» на экране согласия
        db.clear_pd_consent(m, target)
        at = _app(member)
        assert _gated(at)
        next(b for b in at.button if b.key == "pd_consent_logout").click().run()
        assert not at.exception and at.session_state["user"] is None
        assert any(b.label == "Войти" for b in at.button)
        # вход через форму: сначала экран согласия
        at = _app()
        at.text_input[0].input("pd_ui")
        at.text_input[1].input("pass12345")
        at.button[0].click().run()
        assert not at.exception and _gated(at)
        # админ без согласия тоже не попадает в панель; после согласия - попадает
        at = _app(admin)
        assert _gated(at) and not at.tabs
        at.checkbox(key="pd_consent_check").check()
        next(b for b in at.button if b.label == "Продолжить").click().run()
        assert not at.exception and any(t.value == "Панель лидера СНО" for t in at.title)
        # админ видит статус согласия участников; «войти как» участника без согласия - не блокируется
        # (согласие нужно от того, кто реально вошёл; участник даст своё сам при входе)
        assert any("ещё не дано" in c.value for c in at.caption)
        at.selectbox(key="imp_pick").set_value(m).run()
        next(b for b in at.button if b.key == "imp_go").click().run()
        assert not at.exception and at.session_state["_imp_admin"]["id"] == admin["id"]
        assert not _gated(at) and any(t.value == "Мои достижения" for t in at.title)
        # «Отметить отзыв согласия» у участника, давшего согласие
        privacy.give_consent(member, True, db_path=target, log=False)
        at = _app(admin)
        btn = next(b for b in at.button if b.key == f"pd_reset_{m}")
        btn.click().run()
        assert not at.exception, at.exception
        db.clear_cache()
        assert privacy.needs_consent(m, target)
        assert any(e["action"] == "pd_consent_reset" for e in audit.list_entries(db_path=target))
    db.delete_user(m, acting_user_id=admin["id"], db_path=target)
    print("  ПДн: экран согласия OK (политика на входе и в сайдбаре, без согласия нет данных, без галочки - ошибка, "
          "после согласия - кабинет/панель, «Выйти», вход через форму, «войти как», отзыв админом)")


def run_dup_number_text(target) -> None:  # noqa: ANN001
    db.init_db(db_path=target, seed_admin=True)
    admin = _admin(target)
    a = db.create_user("dn_a", "pass12345", "Номеров Андрей Петрович", db_path=target)
    b = db.create_user("dn_b", "pass12345", "Чужой Борис Олегович", db_path=target)
    K = lambda c: ach.kind_by_code(c, target)["id"]  # noqa: E731
    R = lambda c: ach.row_by_code(c, target)["id"]  # noqa: E731

    def dok(owner, title, num, d="2026-03-14", topic="Т"):  # noqa: ANN001, ANN202
        return ach.save_achievement({"kind_id": K("doklad"), "owner_id": owner, "row_id": R("I01.ru"), "title": title,
                                     "date_from": d, "topic": topic, "ochno": True, "number": num},
                                    admin, db_path=target)["id"]

    a1 = dok(a, "Всероссийская конференция «Молодёжь и аграрная наука XXI века» (секция 3)", "Р-Н-7001-26")
    a2 = dok(a, "Форум «Дон»", "Р-Н-7001-26", "2026-04-02")
    a3 = dok(a, "Свой", "Р-Н-7002-26")
    b1 = dok(b, "Чужая тайная конференция", "Р-Н-7002-26", "2026-05-05")
    b2 = dok(b, "Мероприятие не указано (импорт из ЛК, уточнить)", "Р-Н-7001-26", "2026-05-06", topic="Работа Б из ЛК")
    db.clear_cache()
    get = lambda i: ach.get_achievement(i, target)  # noqa: E731
    va, vb = {"id": a, "role": "member"}, {"id": b, "role": "member"}
    # без зрителя (так хранится в issues / журнале) - без названий, без «(1)»
    neutral = next(t for t in get(a3)["issues"] if ach.is_dup_number_issue(t))
    assert neutral.startswith("номер «Р-Н-7002-26» уже указан в другой записи") and "(1)" not in neutral, neutral
    assert "Чужая" not in neutral
    n2 = next(t for t in get(a1)["issues"] if ach.is_dup_number_issue(t))
    assert "ещё в 2 записях" in n2 and "Форум" not in n2, n2
    # участник: своя запись - с названием, видом и датой; чужая - «у другого участника»
    t3 = ach.dup_number_text(get(a3), va)
    assert t3 == ("номер «Р-Н-7002-26» уже указан в записи у другого участника - не внесено ли одно "
                  "достижение дважды?"), t3
    t1 = ach.dup_number_text(get(a1), va)
    assert "ещё в 2 записях: «Форум «Дон»» (Доклад, 02.04.2026); 1 запись у другого участника" in t1, t1
    assert "Работа Б" not in t1 and "Чужой" not in t1
    # владелец второй записи видит свою (название из темы вместо заглушки ЛК) и «у другого участника»
    tb = ach.dup_number_text(get(b2), vb)
    assert tb.startswith("номер «Р-Н-7001-26» уже указан ещё в 2 записях у другого участника - "), tb
    assert "Всероссийская" not in tb and "Форум" not in tb
    # админ видит всё: короткое название (…), вид, дата и владелец чужой записи
    tadm = ach.dup_number_text(get(a1), ach.ADMIN_VIEW)
    assert "«Форум «Дон»» (Доклад, 02.04.2026)" in tadm and "«Работа Б из ЛК» (Доклад, 06.05.2026, Чужой Б.О.)" in tadm, tadm
    t3a = ach.dup_number_text(get(b1), ach.ADMIN_VIEW)
    assert "в записи «Свой» (Доклад, 14.03.2026, Номеров А.П.)" in t3a, t3a
    long_ = ach.dup_number_text(get(b2), ach.ADMIN_VIEW)   # длинное название сокращено
    assert "«Всероссийская конференция «Молодёжь и аграрн…»" in long_ and "XXI" not in long_, long_
    assert "…» (Доклад, 14.03.2026, Номеров А.П.)" in long_, long_
    # соавтор публикации видит её название (она видна ему в «Мои достижения»)
    pub = ach.save_achievement({"kind_id": K("publication"), "owner_id": b, "row_id": R("I02.vak"),
                                "title": "Статья с соавтором", "year": 2026,
                                "journal": "Вестник", "number": "Р-Н-7003-26", "owner_share": 50,
                                "coauthors": [{"user_id": a, "name": "Номеров Андрей Петрович", "share": 50}]},
                               admin, db_path=target)["id"]
    a4 = dok(a, "Доклад по статье", "Р-Н-7003-26", "2026-06-01")
    db.clear_cache()
    assert get(pub)["people"] and any(p["user_id"] == a for p in get(pub)["people"]), get(pub)["people"]
    t4 = ach.dup_number_text(get(a4), va)
    assert "в записи «Статья с соавтором» (Публикация" in t4 and "Чужой Б.О." in t4, t4
    # классификация и списки: «Что дозаполнить» (админ) и плашки участника
    assert todo.classify_issue(neutral, "doklad") == "dup_number" and todo.classify_issue(t1, "doklad") == "dup_number"
    td = {r["id"]: r for r in todo.list_todo(owner_id=a, types=["dup_number"], db_path=target, viewer=ach.ADMIN_VIEW)}
    assert a1 in td and "Работа Б из ЛК" in td[a1]["problem_text"] and "dup_number" in td[a1]["problem_codes"]
    recs = ach.list_achievements(owner_id=a, with_coauthored=True, db_path=target)
    mt = todo.member_todo(recs, viewer=va)
    assert set(mt["check"]) >= {a1, a2, a3}
    txt = " ".join(t for _, t in mt["items"][a1]["problems"])
    assert "Форум «Дон»" in txt and "у другого участника" in txt and "Работа Б" not in txt, txt
    assert "Чужая тайная" not in " ".join(t for _, t in mt["items"][a3]["problems"])
    # годовой отчёт (админ): подробный текст
    w = next(x for x in ach.report_data(2026, db_path=target)["warnings"] if x["id"] == a3)
    assert "Чужая тайная конференция" in w["issues"], w

    # интерфейс: плашка у участника и сообщение после сохранения
    consent_all(target)
    with _Env(target):
        at = _app(db.get_user_by_id(a, target))
        mds = " ".join(md.value or "" for md in at.markdown)
        assert "⚠ проверьте: номер «Р-Н-7001-26» уже указан ещё в 2 записях" in mds, mds
        assert "Чужая тайная" not in mds and "Работа Б из ЛК" not in mds and "другой записи (" not in mds, mds
        at.button(key=f"btn_edit_{a3}").click().run()
        at.text_input(key=f"e{a3}_f_title").set_value("Свой (уточнено)")
        at.button(key=f"e{a3}_save").click().run()
        assert not at.exception, at.exception
        msgs = " ".join(w.value for w in at.warning)
        assert "проверьте: номер «Р-Н-7002-26» уже указан в записи у другого участника - не внесено" in msgs, msgs
        # админ: «Что дозаполнить» с подробностями
        ad = _app(admin)
        ad.multiselect(key="td_types").set_value(["dup_number"]).run()
        assert not ad.exception, ad.exception
        mds = " ".join(md.value or "" for md in ad.markdown)
        assert "Чужая тайная конференция" in mds and "Номеров А.П." in mds, mds
    for uid in (a, b):
        db.delete_user(uid, acting_user_id=admin["id"], db_path=target)
    print("  повтор номера: понятный текст OK (название/вид/дата видимой записи, «у другого участника», "
          "соавтор видит публикацию, админ - всё; плашки, сообщение после сохранения, «Что дозаполнить», отчёт)")


def run_all(fresh) -> None:  # noqa: ANN001
    """fresh(): новая пустая база (SQLite-файл или очищенная Postgres)."""
    run_consent_db(fresh())
    run_consent_ui(fresh())
    run_dup_number_text(fresh())


if __name__ == "__main__":
    import tempfile

    tmp = Path(tempfile.mkdtemp())
    counter = iter(range(1000))
    run_all(lambda: tmp / f"sno_pd_{next(counter)}.db")
    print("OK")
