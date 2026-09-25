"""СНО ДГТУ — учёт мероприятий. Streamlit-прототип."""

from __future__ import annotations

import json
from datetime import date

import streamlit as st
import pandas as pd
import plotly.express as px

import auth
import brand
import db
import report
import ru_text
import achievements as ach
import ui_achievements as ua

st.set_page_config(
    page_title=brand.PAGE_TITLE,
    page_icon=str(brand.ICON),
    layout="wide",
)



@st.cache_resource(show_spinner=False)
def _engine():
    """One SQLAlchemy engine (connection pool) per server process."""
    return db.get_engine()


# Bump SCHEMA_VERSION when the schema changes: Streamlit Cloud hot-reloads code on
# push without restarting the process, so a cached init would never re-run.
SCHEMA_VERSION = "2026-09-25-annual"


@st.cache_resource(show_spinner="Подключение к базе данных…")
def _init_schema(version: str = SCHEMA_VERSION) -> bool:
    """Create tables once per server process (not on every rerun).

    init_db retries 3 times with a 2 s backoff: Neon free tier may need a few
    seconds to wake up. New tables (meetings, settings) are added to existing
    databases via CREATE TABLE IF NOT EXISTS, existing data is untouched.
    """
    _engine()
    db.init_db(seed_admin=False)
    return True


try:
    _init_schema(SCHEMA_VERSION)
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
    brand.login_header(brand.sno_name(db))

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


def _show_msgs(key: str) -> None:
    msgs = st.session_state.pop(key, None)
    if not msgs:
        return
    if isinstance(msgs, tuple):
        msgs = [msgs]
    for kind, text_ in msgs:
        getattr(st, kind)(text_)


def member_cabinet(user: dict) -> None:
    st.title("Мои достижения")
    st.caption("Доклады, публикации, конкурсы, гранты, стипендии и другие достижения — "
               "из них собирается годовой отчёт СНО.")

    this_year = date.today().year
    has, summary = ua.year_summary_text(user, this_year)
    (st.success if has else st.info)(summary)

    tab_mine, tab_stats = st.tabs(["Мои достижения", "Статистика СНО"])
    with tab_stats:
        ua.member_stats(_year_choices(include_all=True), _year_label)
    with tab_mine:
        with st.expander("➕ Добавить достижение", expanded=True):
            ua.achievement_form("p_", user)
        ua.member_list(user)


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
    recs = ach.list_achievements(year=y)

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Достижений", len(recs), help="Все записи участников и СНО за период")
    c2.metric("В годовом отчёте", sum(1 for r in recs if r["counted"]),
              help="Записи с указанным уровнем/подпунктом (заочные доклады не считаются)")
    c3.metric("Заседаний", db.count_meetings(y, kind=db.DEFAULT_MEETING_KIND))
    c4.metric(
        "Других мероприятий СНО",
        db.count_meetings(y, exclude_kind=db.DEFAULT_MEETING_KIND),
        help="Конференции, форумы, круглые столы и др. из вкладки «Заседания и мероприятия»",
    )

    st.markdown("#### Достижения по месяцам")
    months = [0] * 12
    for r in recs:
        if r["date_from"] and (not y or r["date_from"][:4] == str(y)):
            months[int(r["date_from"][5:7]) - 1] += 1
    df_month = pd.DataFrame({"Месяц": list(ru_text.MONTHS_SHORT), "Достижений": months})
    fig_month = px.bar(df_month, x="Месяц", y="Достижений")
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
        st.caption("За всё время: достижения суммированы по месяцам всех лет (по дате начала).")

    by_person = ach.stats_by_person(y)
    st.markdown("#### Топ-5 активных")
    top = by_person[:5]
    if top:
        st.markdown("\n".join(
            f"{i}. {r['full_name']} — {ru_text.with_count(r['total'], ua.RECORD_FORMS)}"
            for i, r in enumerate(top, start=1)))
    else:
        st.caption("За выбранный период записей пока нет.")

    st.markdown("#### Участие по мероприятиям")
    events = ach.events_overview(y, with_names=True)
    if events:
        ev_types = sorted({e["kind"] for e in events})
        sel = st.multiselect("Вид", ev_types, key="stats_ev_types", placeholder="Все виды")
        rows = [e for e in events if not sel or e["kind"] in sel]
        st.caption(f"Мероприятий и работ: {len(rows)}, участий членов СНО: {sum(e['people'] for e in rows)}")
        st.dataframe(
            pd.DataFrame([{"Название": e["title"], "Вид": e["kind"], "Дата": ach.fmt_date(e["date"]),
                           "Участников": e["people"], "Кто участвовал": ", ".join(e["names"])}
                          for e in rows],
                         columns=["Название", "Вид", "Дата", "Участников", "Кто участвовал"]),
            hide_index=True, width="stretch")
    else:
        st.caption("За выбранный период мероприятий пока нет.")

    with st.expander("Подробнее"):
        st.markdown("#### По показателям годового отчёта")
        by_ind = ua._counts_by_indicator(y)
        df_ind = pd.DataFrame(by_ind)
        if not df_ind.empty and df_ind["Записей"].sum():
            fig = px.bar(df_ind[df_ind["Записей"] > 0], x="Записей", y="Показатель", orientation="h",
                         text="Записей")
            fig.update_layout(yaxis={"categoryorder": "total ascending"}, margin=dict(t=10, l=10),
                              height=max(260, 30 * int((df_ind["Записей"] > 0).sum()) + 80))
            fig.update_traces(textposition="outside")
            st.plotly_chart(fig, width="stretch")
        st.dataframe(df_ind, hide_index=True, width="stretch")

        st.markdown("#### По участникам")
        if by_person:
            kinds = [k["label"] for k in ach.list_kinds(admin=True, include_hidden=True)]
            used = [k for k in kinds if any(k in r["by_kind"] for r in by_person)]
            st.dataframe(
                pd.DataFrame([{"ФИО": r["full_name"], "Логин": r["login"], "Всего": r["total"],
                               "В отчёте": r["counted"], **{k: r["by_kind"].get(k, 0) for k in used},
                               "С номером / всего": f"{r['with_number']} / {r['total']}"} for r in by_person]),
                width="stretch", hide_index=True)
            df_stack = pd.DataFrame([{"ФИО": r["full_name"], "Вид": k, "Достижений": n}
                                     for r in by_person for k, n in r["by_kind"].items()])
            fig_stack = px.bar(df_stack, x="ФИО", y="Достижений", color="Вид", barmode="stack",
                               title="Достижения участников по видам")
            fig_stack.update_layout(xaxis_tickangle=-30, margin=dict(t=40, b=80))
            st.plotly_chart(fig_stack, width="stretch")
        else:
            st.info("Нет записей за выбранный период.")

        st.markdown("#### Достижения и номера в портфолио")
        if recs:
            st.dataframe(
                pd.DataFrame([
                    {"ФИО": r["owner_name"] or "СНО", "Вид": r["kind_label"], "Название": r["title"],
                     "Дата": ach.fmt_date(r["date_from"]),
                     "Номер в портфолио": r["number"] or "", "Есть номер": bool(r["number"])}
                    for r in recs
                ]),
                width="stretch",
                hide_index=True,
                column_config=_NUMBER_COLUMNS,
            )
        else:
            st.caption("За выбранный период записей нет.")


_NUMBER_COLUMNS = {
    "Номер в портфолио": st.column_config.TextColumn("Номер в портфолио"),
    "Есть номер": st.column_config.CheckboxColumn("Есть номер", disabled=True),
}


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

    st.divider()
    ua.annual_report_section(_year_choices())


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

    st.divider()
    ua.catalog_editor()


def admin_panel(user: dict) -> None:
    st.title("Панель лидера СНО")
    tabs = st.tabs(["Участники", "Статистика", "Заседания и мероприятия", "Отчёт", "Все достижения", "Настройки"])
    with tabs[0]:
        admin_members(user)
    with tabs[1]:
        admin_stats()
    with tabs[2]:
        admin_meetings()
    with tabs[3]:
        admin_report()
    with tabs[4]:
        ua.admin_achievements(user, _year_choices(include_all=True), _year_label)
    with tabs[5]:
        admin_settings()


# ── Main ────────────────────────────────────────────────────────────────────


def main() -> None:
    _ensure_session()
    brand.inject_css()
    brand.sidebar_logo()
    user = st.session_state.user
    if user is None:
        render_login()
    else:
        brand.compact_header(brand.sno_name(db))
        render_sidebar(user)
        if user["role"] == "admin":
            admin_panel(user)
        else:
            member_cabinet(user)
    _apply_cookie_op()


if __name__ == "__main__":
    main()
