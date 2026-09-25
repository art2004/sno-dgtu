"""СНО ДГТУ — учёт мероприятий. Streamlit-прототип."""

from __future__ import annotations

import json
from datetime import date, datetime

import streamlit as st
import pandas as pd
import plotly.express as px

import auth
import db
import report
import ru_text

st.set_page_config(
    page_title="СНО ДГТУ — мероприятия",
    page_icon="🎓",
    layout="wide",
)



@st.cache_resource(show_spinner=False)
def _engine():
    """One SQLAlchemy engine (connection pool) per server process."""
    return db.get_engine()


@st.cache_resource(show_spinner="Подключение к базе данных…")
def _init_schema() -> bool:
    """Create tables once per server process (not on every rerun).

    init_db retries 3 times with a 2 s backoff: Neon free tier may need a few
    seconds to wake up. New tables (meetings, settings) are added to existing
    databases via CREATE TABLE IF NOT EXISTS, existing data is untouched.
    """
    _engine()
    db.init_db(seed_admin=False)
    return True


try:
    _init_schema()
except Exception as exc:  # noqa: BLE001
    st.error(
        "Не удалось подключиться к базе данных. Проверьте DATABASE_URL "
        "(Secrets в Streamlit Cloud или переменную окружения).\n\n"
        f"Ошибка: {exc.__class__.__name__}: {exc}"
    )
    st.stop()


def _session_user(user: dict) -> dict:
    return {
        "id": user["id"],
        "login": user["login"],
        "full_name": user["full_name"],
        "role": user["role"],
    }


def _ensure_session() -> None:
    if "user" not in st.session_state:
        st.session_state.user = None
    # Page refresh = new session: restore login from the signed cookie. It is read
    # server-side from the request (st.context.cookies), so it is available on the
    # very first run — no flash of the login form. After «Выйти» the browser may
    # still have sent the old cookie with this connection, hence the flag.
    if st.session_state.user is None and not st.session_state.get("_cookie_logged_out"):
        user = auth.user_from_token(st.context.cookies.get(auth.COOKIE_NAME))
        if user is not None:
            st.session_state.user = _session_user(user)
    elif st.session_state.user is not None:
        # Role/name/login changed by an admin take effect on the next rerun;
        # a deleted or disabled account is logged out.
        fresh = db.get_user_by_id(st.session_state.user["id"])
        if fresh is None or not fresh.get("active"):
            st.session_state.user = None
            st.session_state["_cookie_logged_out"] = True
            _queue_auth_cookie(None)
        else:
            st.session_state.user = _session_user(fresh)


def _queue_auth_cookie(user_id: int | None) -> None:
    """Schedule setting (fresh token for user_id) or deleting (None) the cookie."""
    token = None
    if user_id is not None:
        user = db.get_user_by_id(user_id)
        token = auth.make_auth_token(user) if user else None
    st.session_state["_cookie_op"] = {"token": token}


def _apply_cookie_op() -> None:
    """Write/delete the cookie in the browser (st.html with JS, no extra package)."""
    op = st.session_state.pop("_cookie_op", None)
    if op is None:
        return
    token = op["token"]
    value, max_age = (token, auth.COOKIE_TTL_SECONDS) if token else ("", 0)
    cookie = f"{auth.COOKIE_NAME}={value}; Max-Age={max_age}; Path=/; SameSite=Lax"
    st.html(
        "<script>document.cookie = " + json.dumps(cookie)
        + " + (location.protocol === 'https:' ? '; Secure' : '');</script>",
        unsafe_allow_javascript=True,
    )


def logout() -> None:
    st.session_state.user = None
    st.session_state["_cookie_logged_out"] = True
    _queue_auth_cookie(None)
    st.rerun()


def render_login() -> None:
    st.title("СНО ДГТУ — учёт мероприятий")
    st.caption("Студенческое научное общество · Донской государственный технический университет")
    st.markdown("---")

    admin_status = db.ensure_admin()
    if admin_status == "missing_password":
        st.error(
            "Администратор ещё не создан: не задан пароль ADMIN_PASSWORD.\n\n"
            "Откройте настройки приложения в Streamlit Community Cloud → "
            "**Settings → Secrets** и добавьте строки:\n\n"
            "```toml\nADMIN_LOGIN = \"admin\"\nADMIN_PASSWORD = \"надёжный-пароль\"\n```\n\n"
            "После сохранения приложение перезапустится, и можно будет войти "
            "с этими данными (затем смените пароль в боковой панели)."
        )

    with st.form("login_form"):
        login = st.text_input("Логин")
        password = st.text_input("Пароль", type="password")
        submitted = st.form_submit_button("Войти", type="primary", width="stretch")
        if submitted:
            if not login or not password:
                st.error("Введите логин и пароль.")
            else:
                user = auth.authenticate(login.strip(), password)
                if user is None:
                    st.error("Неверный логин или пароль, либо учётная запись отключена.")
                else:
                    st.session_state.user = _session_user(user)
                    st.session_state.pop("_cookie_logged_out", None)
                    _queue_auth_cookie(user["id"])
                    st.rerun()


def render_sidebar(user: dict) -> None:
    with st.sidebar:
        st.markdown(f"**{user['full_name']}**")
        role_label = "Лидер СНО (админ)" if user["role"] == "admin" else "Член совета"
        st.caption(f"{role_label} · @{user['login']}")
        if st.button("Выйти", width="stretch"):
            logout()
        st.markdown("---")
        with st.expander("🔑 Сменить пароль"):
            with st.form("change_password_form", clear_on_submit=True):
                current = st.text_input("Текущий пароль", type="password")
                new = st.text_input(
                    f"Новый пароль (не менее {auth.MIN_PASSWORD_LENGTH} символов)",
                    type="password",
                )
                repeat = st.text_input("Повторите новый пароль", type="password")
                if st.form_submit_button("Сменить пароль", width="stretch"):
                    ok, msg = auth.change_password(user["id"], current, new, repeat)
                    (st.success if ok else st.error)(msg)
                    if ok:  # old cookies are now invalid → fresh one for this browser
                        _queue_auth_cookie(user["id"])
        st.caption(f"База данных: {db.backend_name()}")


# ── Member views ────────────────────────────────────────────────────────────


TYPE_LABELS = {
    "грант": "грант",
    "конференция": "конференция",
    "конкурс": "конкурс",
    "стипендия": "стипендия",
    "статья": "статья",
}
_P_FIELDS = {"title": "", "topic": "", "ach": ""}


def _add_participation_cb(user_id: int | None, pfx: str = "p_") -> None:
    """on_click callback: runs before the rerun, so it may reset the input widgets.
    user_id None → admin form: the member is taken from the «<pfx>user» selectbox."""
    ss = st.session_state
    on_behalf = user_id is None
    if on_behalf:
        user_id = ss.get(f"{pfx}user")
        if not user_id:
            ss[f"{pfx}msg"] = ("error", "Выберите участника.")
            return
    etype = ss.get(f"{pfx}type", db.EVENT_TYPES[0])
    title = (ss.get(f"{pfx}title") or "").strip()
    is_article = etype == db.ARTICLE_TYPE
    if not title:
        ss[f"{pfx}msg"] = ("error", "Укажите название статьи." if is_article else "Укажите название мероприятия.")
        return
    try:
        res = db.add_participation_ex(
            user_id,
            title,
            etype,
            ss.get(f"{pfx}date") or date.today(),
            article_topic=ss.get(f"{pfx}topic") if is_article else None,
            indexing=ss.get(f"{pfx}indexing") if is_article else None,
            achievement_number=ss.get(f"{pfx}ach"),
        )
    except db.DuplicateError:
        ss[f"{pfx}msg"] = ("warning", ("Участник уже зарегистрирован" if on_behalf else "Вы уже зарегистрированы")
                           + " на это мероприятие (одинаковые название, тип и дата).")
        return
    except ValueError as e:
        ss[f"{pfx}msg"] = ("error", str(e))
        return
    msgs = [("success", "Статья добавлена." if is_article else "Участие добавлено.")]
    if res["indexing_conflict"]:
        msgs.append(("info", f"Эта статья уже добавлена соавтором с индексацией "
                             f"«{res['indexing_conflict']}» — оставлена она."))
    ss[f"{pfx}msg"] = msgs
    ss.update({f"{pfx}{k}": v for k, v in _P_FIELDS.items()})


def _participation_form(user_id: int | None, pfx: str = "p_") -> None:
    """Add-participation inputs (no st.form: article fields appear as soon as «статья»
    is chosen). Used by members (pfx p_) and by admins on behalf of a member (ap_)."""
    for k, v in _P_FIELDS.items():
        st.session_state.setdefault(f"{pfx}{k}", v)
    st.session_state.setdefault(f"{pfx}date", date.today())
    col1, col2 = st.columns(2)
    with col1:
        event_type = st.selectbox(
            "Тип мероприятия", db.EVENT_TYPES, key=f"{pfx}type",
            format_func=lambda t: TYPE_LABELS.get(t, t),
        )
        st.date_input("Дата", key=f"{pfx}date", format="DD.MM.YYYY")
    is_article = event_type == db.ARTICLE_TYPE
    with col2:
        st.text_input(
            "Название статьи" if is_article else "Название",
            key=f"{pfx}title",
            placeholder="Название статьи" if is_article else "Например: УМНИК 2026",
        )
        if is_article:
            st.text_input("Тема статьи", key=f"{pfx}topic")
            st.selectbox("Индексация", db.INDEXING_OPTIONS, key=f"{pfx}indexing")
    st.text_input(
        "Номер достижения (с сайта вуза, необязательно)",
        key=f"{pfx}ach",
        max_chars=db.ACHIEVEMENT_MAX_LEN,
    )
    st.button("Сохранить", type="primary", key=f"{pfx}save",
              on_click=_add_participation_cb, args=(user_id, pfx))
    _show_msgs(f"{pfx}msg")


def _show_msgs(key: str) -> None:
    msgs = st.session_state.pop(key, None)
    if not msgs:
        return
    if isinstance(msgs, tuple):
        msgs = [msgs]
    for kind, text_ in msgs:
        getattr(st, kind)(text_)


def member_cabinet(user: dict) -> None:
    st.title("Мои мероприятия")
    st.caption("Добавляйте участия в гранты, конференции, конкурсы, стипендии и статьи.")

    this_year = date.today().year
    summary = db.user_year_summary(user["id"], this_year)
    (st.success if summary["total"] else st.info)(
        ru_text.member_year_summary(this_year, summary)
    )

    tab_mine, tab_stats = st.tabs(["Мои участия", "Статистика СНО"])
    with tab_stats:
        member_stats()
    with tab_mine:
        _member_participations(user)


def _member_participations(user: dict) -> None:
    with st.expander("➕ Добавить участие", expanded=True):
        _participation_form(user["id"], "p_")

    rows = db.list_participations_for_user(user["id"])
    st.subheader(f"Список ({len(rows)})")
    _show_msgs("ach_msg")
    if not rows:
        st.info("Пока нет участий. Добавьте первое выше.")
        return

    for r in rows:
        pid = r["participation_id"]
        c1, c2, c3, c4, c5, c6 = st.columns([4, 2, 2, 2, 1, 1])
        c1.write(r["title"])
        if r["type"] == db.ARTICLE_TYPE and (r.get("article_topic") or r.get("indexing")):
            c1.caption(" · ".join(x for x in (r.get("article_topic"), r.get("indexing")) if x))
        c2.write(r["type"])
        c3.write(r["event_date"])
        ach = r.get("achievement_number")
        c4.caption(f"№ достижения: {ach}" if ach else "без номера достижения")
        with c5:
            with st.popover("✏️", help="Номер достижения"):
                new_ach = st.text_input(
                    "Номер достижения (с сайта вуза)",
                    value=ach or "",
                    key=f"ach_{pid}",
                    max_chars=db.ACHIEVEMENT_MAX_LEN,
                )
                if st.button("Сохранить номер", key=f"ach_save_{pid}"):
                    try:
                        db.set_achievement_number(pid, new_ach, user_id=user["id"])
                        st.session_state["ach_msg"] = ("success", "Номер достижения сохранён.")
                        st.rerun()
                    except ValueError as e:
                        st.error(str(e))
        with c6:
            if st.button("🗑", key=f"del_p_{pid}", help="Удалить"):
                st.session_state[f"confirm_del_p_{pid}"] = True

        confirm_key = f"confirm_del_p_{pid}"
        if st.session_state.get(confirm_key):
            st.warning(f"Удалить участие в «{r['title']}»?")
            b1, b2, _ = st.columns([1, 1, 4])
            if b1.button("Да, удалить", key=f"yes_p_{pid}", type="primary"):
                db.delete_participation(pid, user_id=user["id"])
                st.session_state.pop(confirm_key, None)
                st.rerun()
            if b2.button("Отмена", key=f"no_p_{pid}"):
                st.session_state.pop(confirm_key, None)
                st.rerun()


def member_stats() -> None:
    """СНО-wide statistics for members: events only — no names, logins or numbers."""
    st.subheader("Статистика СНО")
    choices = _year_choices(include_all=True)
    year = st.selectbox("Период", choices, index=choices.index(date.today().year),
                        format_func=_year_label, key="m_stats_year")
    y = year or None
    by_event = db.stats_by_event(y)
    c1, c2 = st.columns(2)
    c1.metric("Мероприятий", len(by_event))
    c2.metric("Участий", sum(r["participants_count"] for r in by_event))
    if not by_event:
        st.info("За выбранный период мероприятий пока нет.")
        return

    counts = {t: {"events": 0, "parts": 0} for t in db.EVENT_TYPES}
    for r in by_event:
        if r["type"] in counts:
            counts[r["type"]]["events"] += 1
            counts[r["type"]]["parts"] += r["participants_count"]
    df_type = pd.DataFrame(
        [{"Тип": t, "Мероприятий": c["events"], "Участий": c["parts"]} for t, c in counts.items()]
    )
    st.markdown("#### По типам мероприятий")
    g1, g2 = st.columns([3, 2])
    with g1:
        fig = px.bar(df_type, x="Тип", y="Мероприятий", text="Мероприятий")
        fig.update_layout(height=280, margin=dict(t=10, b=10, l=10, r=10), xaxis_title=None,
                          yaxis_title=None, yaxis=dict(fixedrange=True, rangemode="tozero"),
                          xaxis=dict(fixedrange=True))
        fig.update_traces(textposition="outside", hovertemplate="%{x}: %{y}<extra></extra>")
        st.plotly_chart(fig, width="stretch", config={"displayModeBar": False}, key="m_stats_chart")
    with g2:
        st.dataframe(df_type, hide_index=True, width="stretch")

    st.markdown("#### Мероприятия")
    types = st.multiselect("Тип", db.EVENT_TYPES, key="m_stats_types",
                           placeholder="Все типы")
    rows = [r for r in by_event if not types or r["type"] in types]
    st.dataframe(
        pd.DataFrame(
            [{"Название": r["title"], "Тип": r["type"], "Дата": str(r["event_date"]),
              "Участников": r["participants_count"]} for r in rows],
            columns=["Название", "Тип", "Дата", "Участников"],
        ),
        hide_index=True,
        width="stretch",
    )


# ── Admin: members ──────────────────────────────────────────────────────────


def _role_label(role: str) -> str:
    return "Член совета" if role == "member" else "Админ"


def _save_user(u: dict, acting: dict, fields: dict) -> None:
    """update_user with db-level admin safeguards; messages via session_state."""
    try:
        db.update_user(u["id"], acting_user_id=acting["id"], **fields)
    except db.DuplicateError:
        st.session_state[f"user_msg_{u['id']}"] = ("error", "Логин уже занят.")
        return
    except ValueError as e:  # AdminGuardError and validation
        st.session_state[f"user_msg_{u['id']}"] = ("error", str(e))
        return
    if u["id"] == acting["id"] and fields.get("password"):
        _queue_auth_cookie(acting["id"])  # keep own login
    msg = "Сохранено."
    if fields.get("role") and fields["role"] != u["role"]:
        msg += f" Роль: {_role_label(fields['role']).lower()} (применится при следующем действии пользователя)."
    st.session_state[f"user_msg_{u['id']}"] = ("success", msg)


def admin_members(user: dict) -> None:
    st.subheader("Участники СНО")

    with st.expander("➕ Добавить участника", expanded=False):
        with st.form("add_member"):
            full_name = st.text_input("ФИО")
            login = st.text_input("Логин")
            password = st.text_input("Пароль", type="password")
            role = st.selectbox("Роль", ["member", "admin"], format_func=_role_label)
            if st.form_submit_button("Создать", type="primary"):
                if not full_name.strip() or not login.strip() or not password:
                    st.error("Заполните ФИО, логин и пароль.")
                else:
                    try:
                        db.create_user(login.strip(), password, full_name.strip(), role)
                        st.success(f"Участник «{full_name.strip()}» создан.")
                        st.rerun()
                    except db.DuplicateError:
                        st.error("Логин уже занят.")

    with st.expander("📥 Импорт из Excel", expanded=False):
        _import_members_ui()

    users = db.list_users()
    st.markdown(f"**Всего:** {len(users)}")

    for u in users:
        active_mark = "✅" if u["active"] else "⛔"
        role_ru = "админ" if u["role"] == "admin" else "член совета"
        with st.expander(f"{active_mark} {u['full_name']} (@{u['login']}, {role_ru})"):
            with st.form(f"edit_user_{u['id']}"):
                new_name = st.text_input("ФИО", value=u["full_name"])
                new_login = st.text_input("Логин", value=u["login"])
                new_password = st.text_input(
                    "Новый пароль (оставьте пустым, чтобы не менять)",
                    type="password",
                    key=f"pwd_{u['id']}",
                )
                new_role = st.selectbox(
                    "Роль", ["member", "admin"], index=0 if u["role"] == "member" else 1,
                    format_func=_role_label, key=f"role_{u['id']}",
                    disabled=u["id"] == user["id"],
                    help="Свою роль изменить нельзя." if u["id"] == user["id"] else None,
                )
                new_active = st.checkbox("Активен", value=bool(u["active"]))
                col_save, col_del = st.columns(2)
                save = col_save.form_submit_button("Сохранить")
                delete = col_del.form_submit_button("Удалить")

                if save:
                    if u["id"] == user["id"] and not new_active:
                        st.error("Нельзя отключить свою учётную запись.")
                    else:
                        fields = dict(
                            full_name=new_name,
                            login=new_login,
                            password=new_password if new_password else None,
                            active=new_active,
                            role=None if u["id"] == user["id"] else new_role,
                        )
                        if new_role == "admin" and u["role"] != "admin" and u["id"] != user["id"]:
                            st.session_state[f"confirm_role_{u['id']}"] = fields  # ask first
                        else:
                            _save_user(u, user, fields)
                        st.rerun()

                if delete:
                    if u["id"] == user["id"]:
                        st.error("Нельзя удалить себя.")
                    else:
                        st.session_state[f"confirm_del_u_{u['id']}"] = True

            _show_msgs(f"user_msg_{u['id']}")
            role_key = f"confirm_role_{u['id']}"
            if st.session_state.get(role_key):
                st.warning(
                    f"Назначить «{u['full_name']}» админом? Админ видит всех участников, "
                    "статистику и может менять любые данные."
                )
                b1, b2, _ = st.columns([2, 1, 3])
                if b1.button("Да, назначить админом", key=f"yes_role_{u['id']}", type="primary"):
                    _save_user(u, user, st.session_state.pop(role_key))
                    st.rerun()
                if b2.button("Отмена", key=f"no_role_{u['id']}"):
                    st.session_state.pop(role_key, None)
                    st.rerun()

            confirm_key = f"confirm_del_u_{u['id']}"
            if st.session_state.get(confirm_key):
                st.warning(
                    f"Удалить «{u['full_name']}» и все их участия? Это необратимо."
                )
                b1, b2, _ = st.columns([1, 1, 4])
                if b1.button("Да, удалить", key=f"yes_u_{u['id']}", type="primary"):
                    st.session_state.pop(confirm_key, None)
                    try:
                        db.delete_user(u["id"], acting_user_id=user["id"])
                    except ValueError as e:  # last active admin / yourself
                        st.session_state[f"user_msg_{u['id']}"] = ("error", str(e))
                    st.rerun()
                if b2.button("Отмена", key=f"no_u_{u['id']}"):
                    st.session_state.pop(confirm_key, None)
                    st.rerun()


def _import_members_ui() -> None:
    st.caption(
        "Файл .xlsx, в первой строке заголовки: **ФИО**, **Логин**, **Пароль**. "
        "Участники создаются с ролью «член совета»; существующие логины "
        "пропускаются, их данные не меняются."
    )
    _show_msgs("import_msg")
    up = st.file_uploader("Файл Excel (.xlsx)", type=["xlsx"], key="import_xlsx")
    if up is None:
        return
    try:
        rows = db.parse_members_xlsx(up.getvalue())
    except ValueError as exc:
        st.error(str(exc))
        return
    if not rows:
        st.info("В файле нет строк с участниками.")
        return
    st.dataframe(
        pd.DataFrame(
            [{"Строка": r["row"], "ФИО": r["full_name"], "Логин": r["login"],
              "Статус": r["status"]} for r in rows]
        ),
        hide_index=True,
        width="stretch",
    )
    n_new = sum(r["ok"] for r in rows)
    if n_new == 0:
        st.info("Новых участников для создания нет.")
        return
    label = f"Создать {n_new} {ru_text.plural(n_new, ('участника', 'участников', 'участников'))}"
    if st.button(label, type="primary", key="import_go"):
        with st.spinner("Создаю учётные записи…"):
            created, skipped = db.import_members(rows)
        st.session_state["import_msg"] = (
            "success", f"Импорт завершён: создано {created}, пропущено {skipped}."
        )
        st.rerun()


# ── Admin: stats ────────────────────────────────────────────────────────────

ALL_TIME = 0  # sentinel for «За всё время» in year selectors


def _year_choices(include_all: bool = False) -> list[int]:
    years = set(db.available_years())
    years.add(date.today().year)
    out = sorted(years, reverse=True)
    return out + [ALL_TIME] if include_all else out


def _year_label(y: int) -> str:
    return "За всё время" if y == ALL_TIME else str(y)


def admin_stats() -> None:
    st.subheader("Статистика")

    choices = _year_choices(include_all=True)
    year = st.selectbox(
        "Период",
        choices,
        index=choices.index(date.today().year),
        format_func=_year_label,
        key="stats_year",
    )
    y = year or None  # None → без фильтра

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Участий", db.count_participations(y))
    c2.metric("Мероприятий", db.count_events(y), help="Мероприятия участников (без дублей)")
    c3.metric("Заседаний", db.count_meetings(y, kind=db.DEFAULT_MEETING_KIND))
    c4.metric(
        "Других мероприятий СНО",
        db.count_meetings(y, exclude_kind=db.DEFAULT_MEETING_KIND),
        help="Конференции, форумы, круглые столы и др. из вкладки «Заседания и мероприятия»",
    )

    st.markdown("#### Участия по месяцам")
    months = db.participations_by_month(y)
    df_month = pd.DataFrame({"Месяц": list(ru_text.MONTHS_SHORT), "Участий": months})
    fig_month = px.bar(df_month, x="Месяц", y="Участий")
    fig_month.update_layout(
        height=280,
        margin=dict(t=10, b=10, l=10, r=10),
        xaxis_title=None,
        yaxis_title=None,
        showlegend=False,
        xaxis=dict(categoryorder="array", categoryarray=list(ru_text.MONTHS_SHORT), fixedrange=True),
        yaxis=dict(fixedrange=True, rangemode="tozero", dtick=max(1, (max(months) or 1) // 5)),
    )
    fig_month.update_traces(hovertemplate="%{x}: %{y}<extra></extra>")
    st.plotly_chart(fig_month, width="stretch", config={"displayModeBar": False})
    if year == ALL_TIME:
        st.caption("За всё время: участия суммированы по месяцам всех лет.")

    by_person = db.stats_by_person(y)
    st.markdown("#### Топ-5 активных")
    top = [r for r in by_person if r["total"] > 0][:5]
    if top:
        st.markdown(
            "\n".join(
                f"{i}. {r['full_name']} — {ru_text.with_count(r['total'], ru_text.PARTICIPATION_FORMS)}"
                for i, r in enumerate(top, start=1)
            )
        )
    else:
        st.caption("За выбранный период участий пока нет.")

    with st.expander("Подробнее"):
        _stats_details(by_person, db.stats_by_type(y), db.stats_by_event(y))
        st.markdown("#### Участия и номера в портфолио")
        df_, dt_ = db.year_range(y)
        parts = db.list_all_participations(date_from=df_, date_to=dt_)
        if parts:
            st.dataframe(
                pd.DataFrame([
                    {"ФИО": r["full_name"], "Мероприятие": r["title"], "Тип": r["type"],
                     "Дата": str(r["event_date"]),
                     "Номер в портфолио": r.get("achievement_number") or "",
                     "Есть номер": bool(r.get("achievement_number"))}
                    for r in parts
                ]),
                width="stretch",
                hide_index=True,
                column_config=_NUMBER_COLUMNS,
            )
        else:
            st.caption("За выбранный период участий нет.")
        st.markdown("#### Статьи по индексации")
        by_idx = db.stats_articles_by_indexing(y)
        if by_idx:
            st.dataframe(
                [{"Индексация": r["indexing"], "Статей": r["articles"], "Авторов (участий)": r["authors"]}
                 for r in by_idx],
                width="stretch",
                hide_index=True,
            )
        else:
            st.caption("Статей за выбранный период нет.")


def _stats_details(by_person: list[dict], by_type: list[dict], by_event: list[dict]) -> None:
    st.metric("Активных членов совета", len(by_person))

    # ── Charts ──────────────────────────────────────────────────────────────
    st.markdown("#### Графики")
    if by_person or by_type or by_event:
        g1, g2 = st.columns(2)

        with g1:
            if by_person:
                df_person = pd.DataFrame(
                    [
                        {
                            "ФИО": r["full_name"],
                            "Участий": int(r["total"] or 0),
                        }
                        for r in by_person
                    ]
                )
                fig_person = px.bar(
                    df_person,
                    x="ФИО",
                    y="Участий",
                    title="Участия по членам совета",
                    text="Участий",
                )
                fig_person.update_layout(
                    xaxis_tickangle=-30,
                    margin=dict(t=40, b=80),
                    showlegend=False,
                )
                fig_person.update_traces(textposition="outside")
                st.plotly_chart(fig_person, width="stretch")
            else:
                st.info("Нет данных по членам совета для графика.")

        with g2:
            if by_type:
                df_type = pd.DataFrame(
                    [
                        {
                            "Тип": r["type"],
                            "Участий": int(r["participations_count"] or 0),
                            "Мероприятий": int(r["events_count"] or 0),
                        }
                        for r in by_type
                    ]
                )
                fig_type = px.pie(
                    df_type,
                    names="Тип",
                    values="Участий",
                    title="Участия по типам мероприятий",
                    hole=0.35,
                )
                fig_type.update_traces(textposition="inside", textinfo="percent+label")
                st.plotly_chart(fig_type, width="stretch")
            else:
                st.info("Нет данных по типам для графика.")

        if by_event:
            df_event = pd.DataFrame(
                [
                    {
                        "Мероприятие": f'{r["title"]} ({r["event_date"]})',
                        "Тип": r["type"],
                        "Участников": int(r["participants_count"] or 0),
                    }
                    for r in by_event
                ]
            ).sort_values("Участников", ascending=False).head(15)
            fig_event = px.bar(
                df_event,
                x="Участников",
                y="Мероприятие",
                color="Тип",
                orientation="h",
                title="Топ мероприятий по числу участников",
                text="Участников",
            )
            fig_event.update_layout(
                yaxis={"categoryorder": "total ascending"},
                margin=dict(t=40, l=10),
                height=max(320, 28 * len(df_event) + 100),
            )
            fig_event.update_traces(textposition="outside")
            st.plotly_chart(fig_event, width="stretch")

        if by_person:
            df_stack = pd.DataFrame(
                [
                    {
                        "ФИО": r["full_name"],
                        "Гранты": int(r["grants"] or 0),
                        "Конференции": int(r["conferences"] or 0),
                        "Конкурсы": int(r["contests"] or 0),
                        "Стипендии": int(r["scholarships"] or 0),
                        "Статьи": int(r["articles"] or 0),
                    }
                    for r in by_person
                ]
            )
            df_melt = df_stack.melt(
                id_vars="ФИО", var_name="Тип", value_name="Участий"
            )
            fig_stack = px.bar(
                df_melt,
                x="ФИО",
                y="Участий",
                color="Тип",
                title="Участия членов совета по типам",
                barmode="stack",
            )
            fig_stack.update_layout(xaxis_tickangle=-30, margin=dict(t=40, b=80))
            st.plotly_chart(fig_stack, width="stretch")
    else:
        st.info("Пока нет данных для графиков — добавьте участия.")

    st.markdown("#### По участникам")
    if by_person:
        st.dataframe(
            [
                {
                    "ФИО": r["full_name"],
                    "Логин": r["login"],
                    "Всего": r["total"] or 0,
                    "Гранты": r["grants"] or 0,
                    "Конференции": r["conferences"] or 0,
                    "Конкурсы": r["contests"] or 0,
                    "Стипендии": r["scholarships"] or 0,
                    "Статьи": r["articles"] or 0,
                    "С номером / всего": f"{r['with_number']} / {r['total'] or 0}",
                }
                for r in by_person
            ],
            width="stretch",
            hide_index=True,
        )
    else:
        st.info("Нет активных членов совета.")

    st.markdown("#### По типам мероприятий")
    if by_type:
        st.dataframe(
            [
                {
                    "Тип": r["type"],
                    "Мероприятий": r["events_count"],
                    "Участий": r["participations_count"],
                }
                for r in by_type
            ],
            width="stretch",
            hide_index=True,
        )
    else:
        st.info("Мероприятий пока нет.")

    st.markdown("#### По мероприятиям")
    if by_event:
        st.dataframe(
            [
                {
                    "Название": r["title"],
                    "Тип": r["type"],
                    "Дата": r["event_date"],
                    "Участников": r["participants_count"],
                }
                for r in by_event
            ],
            width="stretch",
            hide_index=True,
        )


# ── Admin: all participations ───────────────────────────────────────────────


def admin_all_participations() -> None:
    st.subheader("Все участия")

    with st.expander("➕ Добавить участие за участника"):
        _admin_add_for_member()

    members = db.list_users(active_only=False, role="member")
    member_options = {0: "— все —"}
    member_options.update({m["id"]: f"{m['full_name']} (@{m['login']})" for m in members})

    events = db.stats_by_event()
    event_options = {0: "— все —"}
    event_options.update(
        {
            e["event_id"]: f"{e['title']} ({e['type']}, {e['event_date']})"
            for e in events
        }
    )

    f1, f2, f3, f4, f5, f6 = st.columns([3, 2, 3, 2, 2, 2])
    with f1:
        uid = st.selectbox(
            "Участник",
            options=list(member_options.keys()),
            format_func=lambda x: member_options[x],
            key="adm_filter_user",
        )
    with f2:
        etype = st.selectbox("Тип", ["— все —", *db.EVENT_TYPES])
    with f3:
        eid = st.selectbox(
            "Мероприятие",
            options=list(event_options.keys()),
            format_func=lambda x: event_options[x],
        )
    with f4:
        date_from = st.date_input("Дата с", value=None)
    with f5:
        date_to = st.date_input("Дата по", value=None)
    with f6:
        st.write("")
        no_ach = st.checkbox("Без номера достижения", key="filter_no_ach")

    rows = db.list_all_participations(
        user_id=uid if uid else None,
        event_type=None if etype == "— все —" else etype,
        event_id=eid if eid else None,
        date_from=date_from.isoformat() if date_from else None,
        date_to=date_to.isoformat() if date_to else None,
        without_achievement=no_ach,
    )

    st.caption(f"Найдено: {len(rows)}")
    if not rows:
        st.info("Нет записей по выбранным фильтрам.")
        return

    st.dataframe(
        pd.DataFrame(
            [
                {
                    "ФИО": r["full_name"],
                    "Логин": r["login"],
                    "Мероприятие": r["title"],
                    "Тип": r["type"],
                    "Дата": str(r["event_date"]),
                    "Номер в портфолио": r.get("achievement_number") or "",
                    "Есть номер": bool(r.get("achievement_number")),
                    "Индексация": r.get("indexing") or "",
                    "Добавлено": r["joined_at"][:19],
                }
                for r in rows
            ]
        ),
        width="stretch",
        hide_index=True,
        column_config=_NUMBER_COLUMNS,
    )

    st.markdown("#### Редактирование и удаление участия")
    ids = {r["participation_id"]: f"{r['full_name']} — {r['title']} ({r['event_date']})" for r in rows}
    if st.session_state.get("adm_pick") not in (0, *ids):
        st.session_state["adm_pick"] = 0  # record deleted / filtered out
    pick = st.selectbox("Выберите запись", options=[0, *ids.keys()],
                        format_func=lambda x: "—" if x == 0 else ids[x], key="adm_pick")
    _show_msgs("adm_p_msg")
    if pick:
        _admin_edit_participation(next(r for r in rows if r["participation_id"] == pick))


_NUMBER_COLUMNS = {
    "Номер в портфолио": st.column_config.TextColumn("Номер в портфолио"),
    "Есть номер": st.column_config.CheckboxColumn("Есть номер", disabled=True),
}


def _admin_add_for_member() -> None:
    users = db.list_users(active_only=True)
    opts = {u["id"]: f"{u['full_name']} (@{u['login']})" for u in users}
    if st.session_state.get("ap_user") not in (0, *opts):
        st.session_state["ap_user"] = 0
    st.selectbox("Участник", [0, *opts], key="ap_user",
                 format_func=lambda x: "— выберите участника —" if x == 0 else opts[x])
    _participation_form(None, "ap_")


def _admin_edit_participation(r: dict) -> None:
    pid, eid = r["participation_id"], r["event_id"]
    c1, c2 = st.columns([3, 1], vertical_alignment="bottom")
    new_ach = c1.text_input(
        "Номер в портфолио (номер достижения)", value=r.get("achievement_number") or "",
        key=f"adm_ach_{pid}", max_chars=db.ACHIEVEMENT_MAX_LEN,
    )
    if c2.button("Сохранить номер", key=f"adm_ach_save_{pid}"):
        try:
            db.set_achievement_number(pid, new_ach)
            st.session_state["adm_p_msg"] = ("success", "Номер достижения сохранён.")
            st.rerun()
        except ValueError as e:
            st.error(str(e))

    n = db.event_participants_count(eid)
    with st.expander("✏️ Изменить мероприятие (название, тип, дата, тема, индексация)"):
        if n > 1:
            st.warning(f"Мероприятие общее для {n} участников — изменения затронут всех. "
                       "Если такое мероприятие уже есть, записи будут объединены.")
        with st.form(f"adm_ev_{eid}"):
            title = st.text_input("Название", value=r["title"])
            etype = st.selectbox("Тип мероприятия", db.EVENT_TYPES,
                                 index=db.EVENT_TYPES.index(r["type"]) if r["type"] in db.EVENT_TYPES else 0,
                                 format_func=lambda t: TYPE_LABELS.get(t, t))
            ev_date = st.date_input("Дата", value=db._to_date(r["event_date"]), format="DD.MM.YYYY")
            st.caption("Тема и индексация сохраняются только для типа «статья».")
            topic = st.text_input("Тема статьи", value=r.get("article_topic") or "")
            idx_opts = ["", *db.INDEXING_OPTIONS]
            indexing = st.selectbox(
                "Индексация", idx_opts,
                index=idx_opts.index(r["indexing"]) if r.get("indexing") in idx_opts else 0,
                format_func=lambda x: x or "— не указана —",
            )
            if st.form_submit_button("Сохранить мероприятие"):
                try:
                    res = db.update_event(eid, title, etype, ev_date, topic, indexing)
                    st.session_state["adm_p_msg"] = (
                        "success",
                        "Мероприятие объединено с уже существующим." if res["merged"]
                        else "Мероприятие сохранено.",
                    )
                    st.rerun()
                except ValueError as e:
                    st.error(str(e))

    if st.button("🗑 Удалить участие", key=f"adm_del_{pid}"):
        st.session_state["confirm_admin_del_p"] = pid
    if st.session_state.get("confirm_admin_del_p") == pid:
        st.warning(f"Удалить участие «{r['full_name']}» в «{r['title']}»?"
                   + (" Это последний участник — мероприятие тоже исчезнет." if n <= 1 else ""))
        b1, b2, _ = st.columns([1, 1, 4])
        if b1.button("Да, удалить", type="primary", key="admin_yes_del"):
            db.delete_participation(pid)
            st.session_state.pop("confirm_admin_del_p", None)
            st.session_state["adm_p_msg"] = ("success", "Участие удалено.")
            st.rerun()
        if b2.button("Отмена", key="admin_no_del"):
            st.session_state.pop("confirm_admin_del_p", None)
            st.rerun()


# ── Admin: meetings (заседания СНО) ─────────────────────────────────────────

OTHER_LOCATION = "другая локация (ввести ниже)…"
MEETING_FORMS = ("заседание", "заседания", "заседаний")


def _fmt_date(d: date) -> str:
    return d.strftime("%d.%m.%Y")


def admin_meetings() -> None:
    st.subheader("Заседания и мероприятия СНО")
    st.caption(
        "Все мероприятия СНО для отчёта: заседания, а также конференции, форумы, круглые "
        "столы, где выступали члены СНО. В отчёте — единый список по дате."
    )
    ss = st.session_state

    choices = _year_choices()
    if ss.get("meet_year") not in choices:
        ss["meet_year"] = date.today().year
    year = st.selectbox("Год", choices, key="meet_year")

    with st.expander("➕ Добавить заседание / мероприятие", expanded=bool(ss.get("mf_event_id"))):
        _meeting_add_form(year)
    _show_msgs("mf_msg")

    meetings = db.list_meetings(year)
    n_meet = sum(1 for m in meetings if m["kind"] == db.DEFAULT_MEETING_KIND)
    st.markdown(
        f"**{year} год: {ru_text.with_count(len(meetings), ACTIVITY_FORMS)} в отчёте** "
        f"(из них {ru_text.with_count(n_meet, MEETING_FORMS)})"
    )
    if not meetings:
        st.info("За этот год записей пока нет.")
        return

    st.dataframe(
        [
            {
                "№": m["number"],
                "Вид": m["kind"],
                "Дата": _fmt_date(m["meeting_date"]),
                "Время": m["meeting_time"],
                "Локация": m["location"],
                "Тема": m["topic"],
                "Формат": m["format"],
            }
            for m in meetings
        ],
        width="stretch",
        hide_index=True,
    )

    st.markdown("#### Редактирование")
    for m in meetings:
        mid = m["id"]
        short = m["topic"] if len(m["topic"]) <= 70 else m["topic"][:70] + "…"
        with st.expander(
            f"{m['number']}. {_fmt_date(m['meeting_date'])}, {m['meeting_time']} · {m['kind']} — {short}"
        ):
            if m.get("event_id"):
                st.caption("Связано с мероприятием участников (не будет предложено повторно).")
            known, other = db.split_formats(m["format"])
            with st.form(f"edit_meeting_{mid}"):
                c1, c2, c3 = st.columns(3)
                e_date = c1.date_input("Дата", value=m["meeting_date"], format="DD.MM.YYYY")
                e_time = c2.text_input("Время (ЧЧ:ММ)", value=m["meeting_time"])
                kinds = list(db.MEETING_KINDS)
                e_kind = c3.selectbox(
                    "Вид", kinds, index=kinds.index(m["kind"]) if m["kind"] in kinds else 0
                )
                e_loc = st.text_input("Локация", value=m["location"])
                e_topic = st.text_area("Тема заседания", value=m["topic"])
                e_formats = st.multiselect("Формат", db.MEETING_FORMATS, default=known)
                e_other = st.text_input("Другой формат (необязательно)", value=other)
                col_save, col_del = st.columns(2)
                save = col_save.form_submit_button("Сохранить")
                delete = col_del.form_submit_button("Удалить")
                if save:
                    fmt = db.join_formats(e_formats, e_other)
                    if not e_topic.strip() or not e_loc.strip() or not fmt:
                        st.error("Заполните тему, локацию и формат.")
                    else:
                        try:
                            db.update_meeting(mid, e_date, e_time, e_loc, e_topic, fmt, kind=e_kind)
                            st.success("Сохранено.")
                            st.rerun()
                        except ValueError as e:
                            st.error(str(e))
                if delete:
                    ss[f"confirm_del_m_{mid}"] = True

            confirm_key = f"confirm_del_m_{mid}"
            if ss.get(confirm_key):
                st.warning(f"Удалить запись от {_fmt_date(m['meeting_date'])}? Это необратимо.")
                b1, b2, _ = st.columns([1, 1, 4])
                if b1.button("Да, удалить", key=f"yes_m_{mid}", type="primary"):
                    db.delete_meeting(mid)
                    ss.pop(confirm_key, None)
                    st.rerun()
                if b2.button("Отмена", key=f"no_m_{mid}"):
                    ss.pop(confirm_key, None)
                    st.rerun()


ACTIVITY_FORMS = ("запись", "записи", "записей")

_MF_DEFAULTS = {
    "mf_time": db.DEFAULT_MEETING_TIME,
    "mf_kind": db.DEFAULT_MEETING_KIND,
    "mf_loc_other": "",
    "mf_topic": "",
    "mf_formats": [],
    "mf_fmt_other": "",
    "mf_event_id": None,
    "mf_from_event": None,
}


def _mf_reset() -> None:
    st.session_state.update({k: (list(v) if isinstance(v, list) else v) for k, v in _MF_DEFAULTS.items()})
    st.session_state["mf_date"] = date.today()
    st.session_state.pop("mf_loc_pick", None)  # → last used location on next run


def _prefill_from_event() -> None:
    """on_change of «Добавить из мероприятий участников»: prefill the add form."""
    ss = st.session_state
    ev = (ss.get("mf_events_map") or {}).get(ss.get("mf_from_event"))
    if not ev:
        ss["mf_event_id"] = None
        return
    ss["mf_event_id"] = ev["event_id"]
    ss["mf_date"] = db._to_date(ev["event_date"])
    ss["mf_kind"] = ev["suggested_kind"]
    ss["mf_topic"] = ev["suggested_topic"]
    ss["mf_formats"] = list(ev["suggested_formats"])
    ss["mf_fmt_other"] = ""
    ss["mf_loc_pick"] = OTHER_LOCATION
    ss["mf_loc_other"] = ""


def _submit_meeting() -> None:
    ss = st.session_state
    pick = ss.get("mf_loc_pick", OTHER_LOCATION)
    location = (ss.get("mf_loc_other") or "").strip() or ("" if pick == OTHER_LOCATION else pick)
    fmt = db.join_formats(ss.get("mf_formats") or [], ss.get("mf_fmt_other") or "")
    topic = (ss.get("mf_topic") or "").strip()
    if not topic:
        ss["mf_msg"] = ("error", "Укажите тему.")
        return
    if not location:
        ss["mf_msg"] = ("error", "Укажите локацию.")
        return
    if not fmt:
        ss["mf_msg"] = ("error", "Выберите формат или впишите свой.")
        return
    try:
        d = ss.get("mf_date") or date.today()
        db.add_meeting(d, ss.get("mf_time"), location, topic, fmt,
                       kind=ss.get("mf_kind"), event_id=ss.get("mf_event_id"))
    except ValueError as e:
        ss["mf_msg"] = ("error", str(e))
        return
    ss["mf_msg"] = ("success", "Запись добавлена.")
    ss["meet_year"] = d.year
    _mf_reset()


def _meeting_add_form(year: int) -> None:
    ss = st.session_state
    if "mf_date" not in ss:
        _mf_reset()

    events = db.unlinked_member_events(year)
    ss["mf_events_map"] = {e["event_id"]: e for e in events}
    if ss.get("mf_from_event") not in ss["mf_events_map"]:
        ss["mf_from_event"] = None
    if events:
        emap = ss["mf_events_map"]
        st.selectbox(
            "Добавить из мероприятий участников (необязательно)",
            [None, *emap.keys()],
            key="mf_from_event",
            on_change=_prefill_from_event,
            format_func=lambda eid: "— не выбрано —" if eid is None else (
                f"{db._to_date(emap[eid]['event_date']).strftime('%d.%m.%Y')} · {emap[eid]['title']} "
                f"({emap[eid]['type']}, участников: {emap[eid]['participants_count']})"
            ),
        )
    else:
        st.caption(f"Мероприятий участников за {year} год, ещё не внесённых в отчёт, нет.")

    c1, c2, c3 = st.columns(3)
    c1.date_input("Дата", key="mf_date", format="DD.MM.YYYY")
    c2.text_input("Время (ЧЧ:ММ)", key="mf_time")
    c3.selectbox("Вид", db.MEETING_KINDS, key="mf_kind")
    recent = db.recent_locations()
    if recent:
        options = [*recent, OTHER_LOCATION]
        if ss.get("mf_loc_pick") not in options:
            ss["mf_loc_pick"] = options[0]
        st.selectbox("Локация (последние использованные)", options, key="mf_loc_pick")
        st.text_input("Другая локация", key="mf_loc_other",
                      placeholder="Заполните, если нужной локации нет в списке")
    else:
        ss["mf_loc_pick"] = OTHER_LOCATION
        st.text_input("Локация", key="mf_loc_other",
                      placeholder="Например: Главный корпус, 031 ауд.")
    st.text_area("Тема заседания", key="mf_topic")
    st.multiselect("Формат", db.MEETING_FORMATS, key="mf_formats")
    st.text_input("Другой формат (необязательно)", key="mf_fmt_other",
                  placeholder="Например: Международная конференция")
    st.button("Добавить заседание", type="primary", key="mf_submit", on_click=_submit_meeting)


# ── Admin: report ───────────────────────────────────────────────────────────


def admin_report() -> None:
    st.subheader("Отчёт о проведенных заседаниях")
    choices = _year_choices()
    year = st.selectbox("Год отчёта", choices, key="report_year")
    settings = db.get_report_settings()
    meetings = db.list_meetings(year)

    if settings["sno_name"] == db.DEFAULT_SNO_NAME or not any(
        s["name"] for s in settings["signatories"]
    ):
        st.info("Проверьте название СНО и подписантов во вкладке «Настройки».")

    if meetings:
        st.caption(
            f"В отчёт попадёт {ru_text.with_count(len(meetings), ACTIVITY_FORMS)} "
            "(заседания и другие мероприятия, единая нумерация по дате). "
            f"Заголовок: «Отчет о проведенных заседаниях СНО «{settings['sno_name']}» в {year} году»."
        )
    else:
        st.warning(
            f"За {year} год заседаний и мероприятий нет — в документе будет только шапка "
            "таблицы. Добавьте их во вкладке «Заседания и мероприятия»."
        )

    try:
        docx_bytes = report.build_meetings_docx(
            meetings,
            year,
            settings["sno_name"],
            settings["appendix_label"],
            settings["signatories"],
        )
    except Exception as exc:  # noqa: BLE001
        st.error(f"Не удалось сформировать отчёт: {exc.__class__.__name__}: {exc}")
        docx_bytes = None
    if docx_bytes:
        st.download_button(
            "⬇️ Скачать отчёт (.docx)",
            data=docx_bytes,
            file_name=f"Отчет_о_заседаниях_СНО_{year}.docx",
            mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            type="primary",
            key="dl_report_docx",
        )

    st.markdown("#### Выгрузка в Excel")
    df, dt = db.year_range(year)
    parts = db.list_all_participations(date_from=df, date_to=dt)
    st.caption(
        f"Все участия за {year} год ({len(parts)}) и список заседаний — на отдельных листах."
    )
    st.download_button(
        "⬇️ Участия за год (.xlsx)",
        data=report.build_year_xlsx(parts, meetings),
        file_name=f"Участия_СНО_{year}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        key="dl_year_xlsx",
    )


# ── Admin: report settings ──────────────────────────────────────────────────


def admin_settings() -> None:
    st.subheader("Настройки отчёта")
    s = db.get_report_settings()
    with st.form("report_settings"):
        sno_name = st.text_input(
            "Название СНО (то, что в кавычках «…» в заголовке отчёта)", value=s["sno_name"]
        )
        appendix = st.text_input("Надпись над заголовком", value=s["appendix_label"])
        st.markdown("**Подписанты** (строки можно добавлять и удалять)")
        sig_df = pd.DataFrame(
            s["signatories"] or [{"position": "", "name": ""}], columns=["position", "name"]
        )
        edited = st.data_editor(
            sig_df,
            num_rows="dynamic",
            width="stretch",
            hide_index=True,
            key="signatories_editor",
            column_config={
                "position": st.column_config.TextColumn("Должность", width="large"),
                "name": st.column_config.TextColumn("ФИО (кратко, напр. Вершинина А.В.)"),
            },
        )
        if st.form_submit_button("Сохранить настройки", type="primary"):
            rows = edited.fillna("").to_dict("records")
            db.save_report_settings(sno_name, appendix, rows)
            st.success("Настройки сохранены.")
            st.rerun()

    st.markdown("**Как будут выглядеть подписи:**")
    for sig in s["signatories"]:
        st.text(f"{sig['position']} {report.SIGNATURE_LINE}/{sig['name']}")


def admin_panel(user: dict) -> None:
    st.title("Панель лидера СНО")
    tabs = st.tabs(["Участники", "Статистика", "Заседания и мероприятия", "Отчёт", "Все участия", "Настройки"])
    with tabs[0]:
        admin_members(user)
    with tabs[1]:
        admin_stats()
    with tabs[2]:
        admin_meetings()
    with tabs[3]:
        admin_report()
    with tabs[4]:
        admin_all_participations()
    with tabs[5]:
        admin_settings()


# ── Main ────────────────────────────────────────────────────────────────────


def main() -> None:
    _ensure_session()
    user = st.session_state.user
    if user is None:
        render_login()
    else:
        render_sidebar(user)
        if user["role"] == "admin":
            admin_panel(user)
        else:
            member_cabinet(user)
    _apply_cookie_op()


if __name__ == "__main__":
    main()
