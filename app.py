"""СНО ДГТУ — учёт мероприятий. Streamlit-прототип."""

from __future__ import annotations

from datetime import date, datetime

import streamlit as st
import pandas as pd
import plotly.express as px

import auth
import db

st.set_page_config(
    page_title="СНО ДГТУ — мероприятия",
    page_icon="🎓",
    layout="wide",
)



@st.cache_resource(show_spinner=False)
def _init_schema() -> bool:
    """Create tables once per server process (not on every rerun)."""
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


def _ensure_session() -> None:
    if "user" not in st.session_state:
        st.session_state.user = None


def logout() -> None:
    st.session_state.user = None
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
                    st.session_state.user = {
                        "id": user["id"],
                        "login": user["login"],
                        "full_name": user["full_name"],
                        "role": user["role"],
                    }
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
        st.caption(f"База данных: {db.backend_name()}")


# ── Member views ────────────────────────────────────────────────────────────


def member_cabinet(user: dict) -> None:
    st.title("Мои мероприятия")
    st.caption("Добавляйте участия в гранты, конференции и конкурсы.")

    with st.expander("➕ Добавить участие", expanded=True):
        with st.form("add_participation"):
            col1, col2 = st.columns(2)
            with col1:
                event_type = st.selectbox("Тип мероприятия", db.EVENT_TYPES)
                event_date = st.date_input("Дата", value=date.today())
            with col2:
                title = st.text_input("Название", placeholder="Например: УМНИК 2026")
            submitted = st.form_submit_button("Сохранить", type="primary")
            if submitted:
                if not title or not title.strip():
                    st.error("Укажите название мероприятия.")
                else:
                    try:
                        db.add_participation(
                            user["id"], title.strip(), event_type, event_date
                        )
                        st.success("Участие добавлено.")
                        st.rerun()
                    except db.DuplicateError:
                        st.warning(
                            "Вы уже зарегистрированы на это мероприятие "
                            "(одинаковые название, тип и дата)."
                        )
                    except ValueError as e:
                        st.error(str(e))

    rows = db.list_participations_for_user(user["id"])
    st.subheader(f"Список ({len(rows)})")
    if not rows:
        st.info("Пока нет участий. Добавьте первое выше.")
        return

    for r in rows:
        c1, c2, c3, c4, c5 = st.columns([3, 2, 2, 2, 1])
        c1.write(r["title"])
        c2.write(r["type"])
        c3.write(r["event_date"])
        c4.caption(f"добавлено {r['joined_at'][:10]}")
        with c5:
            if st.button("🗑", key=f"del_p_{r['participation_id']}", help="Удалить"):
                st.session_state[f"confirm_del_p_{r['participation_id']}"] = True

        confirm_key = f"confirm_del_p_{r['participation_id']}"
        if st.session_state.get(confirm_key):
            st.warning(f"Удалить участие в «{r['title']}»?")
            b1, b2, _ = st.columns([1, 1, 4])
            if b1.button("Да, удалить", key=f"yes_p_{r['participation_id']}", type="primary"):
                db.delete_participation(r["participation_id"], user_id=user["id"])
                st.session_state.pop(confirm_key, None)
                st.rerun()
            if b2.button("Отмена", key=f"no_p_{r['participation_id']}"):
                st.session_state.pop(confirm_key, None)
                st.rerun()


# ── Admin: members ──────────────────────────────────────────────────────────


def admin_members(user: dict) -> None:
    st.subheader("Участники СНО")

    with st.expander("➕ Добавить участника", expanded=False):
        with st.form("add_member"):
            full_name = st.text_input("ФИО")
            login = st.text_input("Логин")
            password = st.text_input("Пароль", type="password")
            role = st.selectbox("Роль", ["member", "admin"], format_func=lambda x: (
                "Член совета" if x == "member" else "Админ"
            ))
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
                new_active = st.checkbox("Активен", value=bool(u["active"]))
                col_save, col_del = st.columns(2)
                save = col_save.form_submit_button("Сохранить")
                delete = col_del.form_submit_button("Удалить")

                if save:
                    if u["id"] == user["id"] and not new_active:
                        st.error("Нельзя отключить свою учётную запись.")
                    else:
                        try:
                            db.update_user(
                                u["id"],
                                full_name=new_name,
                                login=new_login,
                                password=new_password if new_password else None,
                                active=new_active,
                            )
                            st.success("Сохранено.")
                            st.rerun()
                        except db.DuplicateError:
                            st.error("Логин уже занят.")

                if delete:
                    if u["id"] == user["id"]:
                        st.error("Нельзя удалить себя.")
                    else:
                        st.session_state[f"confirm_del_u_{u['id']}"] = True

            confirm_key = f"confirm_del_u_{u['id']}"
            if st.session_state.get(confirm_key):
                st.warning(
                    f"Удалить «{u['full_name']}» и все их участия? Это необратимо."
                )
                b1, b2, _ = st.columns([1, 1, 4])
                if b1.button("Да, удалить", key=f"yes_u_{u['id']}", type="primary"):
                    db.delete_user(u["id"])
                    st.session_state.pop(confirm_key, None)
                    st.rerun()
                if b2.button("Отмена", key=f"no_u_{u['id']}"):
                    st.session_state.pop(confirm_key, None)
                    st.rerun()


# ── Admin: stats ────────────────────────────────────────────────────────────


def admin_stats() -> None:
    st.subheader("Статистика")

    by_person = db.stats_by_person()
    by_type = db.stats_by_type()
    by_event = db.stats_by_event()

    c1, c2, c3 = st.columns(3)
    c1.metric("Активных членов совета", len(by_person))
    c2.metric("Мероприятий", db.count_events())
    total_p = sum(int(r["total"] or 0) for r in by_person)
    c3.metric("Всего участий", total_p)

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

    f1, f2, f3, f4, f5 = st.columns(5)
    with f1:
        uid = st.selectbox(
            "Участник",
            options=list(member_options.keys()),
            format_func=lambda x: member_options[x],
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

    rows = db.list_all_participations(
        user_id=uid if uid else None,
        event_type=None if etype == "— все —" else etype,
        event_id=eid if eid else None,
        date_from=date_from.isoformat() if date_from else None,
        date_to=date_to.isoformat() if date_to else None,
    )

    st.caption(f"Найдено: {len(rows)}")
    if not rows:
        st.info("Нет записей по выбранным фильтрам.")
        return

    st.dataframe(
        [
            {
                "ФИО": r["full_name"],
                "Логин": r["login"],
                "Мероприятие": r["title"],
                "Тип": r["type"],
                "Дата": r["event_date"],
                "Добавлено": r["joined_at"][:19],
            }
            for r in rows
        ],
        width="stretch",
        hide_index=True,
    )

    st.markdown("#### Удаление участия")
    ids = {r["participation_id"]: f"{r['full_name']} — {r['title']} ({r['event_date']})" for r in rows}
    pick = st.selectbox("Выберите запись", options=[0, *ids.keys()], format_func=lambda x: "—" if x == 0 else ids[x])
    if pick and st.button("Удалить выбранное участие", type="secondary"):
        st.session_state["confirm_admin_del_p"] = pick
    if st.session_state.get("confirm_admin_del_p") == pick and pick:
        st.warning("Подтвердите удаление.")
        if st.button("Да, удалить", type="primary", key="admin_yes_del"):
            db.delete_participation(pick)
            st.session_state.pop("confirm_admin_del_p", None)
            st.rerun()


def admin_panel(user: dict) -> None:
    st.title("Панель лидера СНО")
    tab1, tab2, tab3 = st.tabs(["Участники", "Статистика", "Все участия"])
    with tab1:
        admin_members(user)
    with tab2:
        admin_stats()
    with tab3:
        admin_all_participations()


# ── Main ────────────────────────────────────────────────────────────────────


def main() -> None:
    _ensure_session()
    user = st.session_state.user
    if user is None:
        render_login()
        return

    render_sidebar(user)
    if user["role"] == "admin":
        admin_panel(user)
    else:
        member_cabinet(user)


if __name__ == "__main__":
    main()
