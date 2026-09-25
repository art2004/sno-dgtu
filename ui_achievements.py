"""Streamlit UI for achievements: add/edit form (вид → подпункт → поля), lists, catalog editor.

No st.form: fields change as soon as the kind / subpoint is chosen. Saving goes through an
on_click callback (runs before the rerun, so inputs can be reset after success)."""

from __future__ import annotations

import re
from datetime import date
from typing import Optional

import streamlit as st

import achievements as ach
import db
import ru_text

RECORD_FORMS = ("запись", "записи", "записей")

NONE = 0
EXTERNAL = "— не из СНО —"


def _k(pfx: str, name: str) -> str:
    return f"{pfx}{name}"


def show_msgs(key: str) -> None:
    msgs = st.session_state.pop(key, None)
    if not msgs:
        return
    if isinstance(msgs, tuple):
        msgs = [msgs]
    for kind, text_ in msgs:
        getattr(st, kind)(text_)


def _members() -> list[dict]:
    return db.list_users(active_only=True)


def _row_label(r: dict) -> str:
    return ach.strip_dash(r["label"])


# ── Prefill / reset ─────────────────────────────────────────────────────────


def _field_keys(pfx: str) -> list[str]:
    return [k for k in st.session_state.keys() if isinstance(k, str) and k.startswith(pfx + "f_")]


def reset_form(pfx: str) -> None:
    for k in _field_keys(pfx):
        del st.session_state[k]
    st.session_state.pop(_k(pfx, "loaded"), None)


def _prefill(pfx: str, rec: dict) -> None:
    """Load a record into widget state once (when the edited record changes)."""
    if st.session_state.get(_k(pfx, "loaded")) == rec["id"]:
        return
    reset_form(pfx)
    ss = st.session_state
    f = lambda n: _k(pfx, "f_" + n)  # noqa: E731
    d = rec["details"]
    ss[_k(pfx, "kind")] = rec["kind_id"]
    ss[f("subpoint")] = rec["subpoint_id"] or NONE
    ss[f("row")] = rec["row_id"] or (ach.NO_INDEX if rec["details"].get("no_index") else NONE)
    ss[f("owner")] = rec["owner_id"] or NONE
    for key, _label, ftype, _req, _extra in ach.FORMS[rec["form"]]:
        if key in ("title", "topic", "number", "link"):
            ss[f(key)] = rec[key] or ""
        elif ftype in ("date", "date_opt"):
            v = rec[key] if key in ("date_from", "date_to") else d.get(key)
            ss[f(key)] = date.fromisoformat(v) if v else (date.today() if ftype == "date" else None)
        elif ftype == "check":
            ss[f(key)] = bool(rec["ochno"])
        elif ftype == "year":
            ss[f(key)] = int(d.get("year") or (rec["date_from"] or str(date.today().year))[:4])
        elif ftype == "ayear":
            ss[f(key)] = d.get("ayear") or ""
        elif ftype == "select":
            ss[f(key)] = d.get(key) or _extra["options"][0]
        elif ftype == "people":
            ss[f("members")] = [p["user_id"] for p in rec["people"] if p["user_id"]]
            ext = [p["name"] for p in rec["people"] if not p["user_id"] and p["name"]]
            ss[f("externals")] = ", ".join(ext + ([rec["externals"]] if rec["externals"] else []))
        elif ftype == "authors":
            ss[f("owner_share")] = rec["owner_share"]
            ss[f("main_pos")] = int(d.get("main_pos") or 1)
            ss[f("n_co")] = len(rec["people"])
            for i, p in enumerate(rec["people"]):
                ss[f(f"co_user_{i}")] = p["user_id"] or NONE
                ss[f(f"co_name_{i}")] = "" if p["user_id"] else p["name"]
                ss[f(f"co_share_{i}")] = p["share"]
        elif ftype == "meeting":
            ss[f("meeting")] = rec["meeting_id"] or d.get("meeting_id") or NONE
        else:
            ss[f(key)] = d.get(key) or ""
    ss[_k(pfx, "loaded")] = rec["id"]


# ── Collect & save ──────────────────────────────────────────────────────────


def _collect(pfx: str, kind: dict, form: str) -> dict:
    ss = st.session_state
    f = lambda n: ss.get(_k(pfx, "f_" + n))  # noqa: E731
    data = {"kind_id": kind["id"], "subpoint_id": f("subpoint") or None, "row_id": f("row") or None,
            "owner_id": f("owner") or None}
    names = {u["id"]: u["full_name"] for u in db.list_users(active_only=False)}
    for key, _label, ftype, _req, _extra in ach.FORMS[form]:
        if ftype == "people":
            data["members"] = f("members") or []
            data["externals"] = f("externals") or ""
        elif ftype == "authors":
            data["owner_share"] = f("owner_share")
            data["main_pos"] = f("main_pos") or 1
            co = []
            for i in range(int(f("n_co") or 0)):
                uid = f(f"co_user_{i}") or None
                name = names.get(uid, "") if uid else (f(f"co_name_{i}") or "")
                co.append({"user_id": uid, "name": name, "share": f(f"co_share_{i}")})
            data["coauthors"] = co
        elif ftype == "meeting":
            data["meeting_id"] = f("meeting") or None
        else:
            data[key] = f(key)
    return data


def _save_cb(pfx: str, acting: dict, record_id: Optional[int], form: str) -> None:
    ss = st.session_state
    kind = ach.get_kind(int(ss.get(_k(pfx, "kind")) or 0))
    if kind is None:
        ss[_k(pfx, "msg")] = ("error", "Выберите вид достижения.")
        return
    try:
        res = ach.save_achievement(_collect(pfx, kind, form), acting, achievement_id=record_id)
    except ach.DuplicateAchievement as e:
        ss[_k(pfx, "msg")] = ("warning", str(e))
        return
    except ValueError as e:
        ss[_k(pfx, "msg")] = ("error", str(e))
        return
    msgs = [("success", "Изменения сохранены." if record_id else f"Добавлено: {kind['label'].lower()}.")]
    if res["issues"]:
        msgs.append(("warning", "Заполните: " + ", ".join(res["issues"])))
    ss[_k(pfx, "msg")] = msgs
    if record_id:
        ss[f"edit_{record_id}"] = False
        ss["mine_msg"] = ss["adm_a_msg"] = msgs
        ss.pop(_k(pfx, "msg"), None)
        reset_form(pfx)
    else:
        keep = {k: ss[k] for k in (_k(pfx, "f_owner"),) if k in ss}
        reset_form(pfx)
        ss.update(keep)


def _meeting_prefill_cb(pfx: str) -> None:
    ss = st.session_state
    mid = ss.get(_k(pfx, "f_meeting"))
    m = db.get_meeting(mid) if mid else None
    if m:
        ss[_k(pfx, "f_title")] = m["topic"] or ss.get(_k(pfx, "f_title"), "")
        ss[_k(pfx, "f_date_from")] = m["meeting_date"]


# ── Form ────────────────────────────────────────────────────────────────────


def achievement_form(pfx: str, acting: dict, record: Optional[dict] = None, on_behalf: bool = False) -> None:
    """Add (record None) or edit form. on_behalf: admin picks the owner (member)."""
    ss = st.session_state
    is_admin = acting.get("role") == "admin"
    if record is not None:
        _prefill(pfx, record)
    kinds = ach.list_kinds(admin=is_admin, include_hidden=record is not None)
    by_id = {k["id"]: k for k in kinds}
    if ss.get(_k(pfx, "kind")) not in by_id:
        ss[_k(pfx, "kind")] = kinds[0]["id"]
    f = lambda n: _k(pfx, "f_" + n)  # noqa: E731

    c1, c2 = st.columns(2)
    with c1:
        kid = st.selectbox("Вид достижения", list(by_id), key=_k(pfx, "kind"),
                           format_func=lambda i: by_id[i]["label"] + (" (СНО)" if by_id[i]["admin_only"] else ""))
    kind = by_id[kid]
    ind_id, row_needed = kind["indicator_id"], True
    with c2:
        if kind["subpoints"] or kind["form"] == "grant":
            sps = {s["id"]: s for s in kind["subpoints"]}
            if ss.get(f("subpoint")) not in (NONE, *sps):
                ss[f("subpoint")] = NONE
            sp_id = st.selectbox("Подпункт", [NONE, *sps], key=f("subpoint"),
                                 format_func=lambda i: "— выберите подпункт —" if i == NONE else sps[i]["label"])
            sp = sps.get(sp_id)
            ind_id = sp["indicator_id"] if sp else None
            row_needed = not (sp and sp["row_id"])
    rows = ach.indicator_rows(ind_id, include_hidden=record is not None) if ind_id else []
    if rows and row_needed:
        dim = next((i["dimension"] for i in ach.catalog() if i["id"] == ind_id), "level")
        row_opts = {r["id"]: r for r in rows}
        if kind["form"] == "publication":
            row_opts[ach.NO_INDEX] = {"id": ach.NO_INDEX, "label": "Без индексации (в годовой отчёт не входит)"}
        if ss.get(f("row")) not in (NONE, *row_opts):
            ss[f("row")] = NONE
        target = c2 if not (kind["subpoints"] or kind["form"] == "grant") else st.container()
        with target:
            st.selectbox("Уровень" if dim == "level" else ("Индексация" if kind["form"] == "publication" else "Подпункт"),
                         [NONE, *row_opts], key=f("row"),
                         format_func=lambda i: "— выберите —" if i == NONE else _row_label(row_opts[i]))
    _, _, form = ach.resolve_target(kind, ss.get(f("subpoint")) or None, None)
    if kind["form"] not in ("grant",) and not kind["subpoints"]:
        form = kind["form"]

    members = _members()
    names = {u["id"]: u["full_name"] for u in members}
    if on_behalf or (is_admin and record is not None):
        own_opts = [NONE, *names]
        if record is not None and record["owner_id"] and record["owner_id"] not in names:
            names[record["owner_id"]] = record["owner_name"] or "?"
            own_opts.append(record["owner_id"])
        if ss.get(f("owner")) not in own_opts:
            ss[f("owner")] = NONE
        st.selectbox("Главный автор" if form == "publication" else "Участник (владелец записи)",
                     own_opts, key=f("owner"),
                     format_func=lambda i: ("— СНО (без участника) —" if kind["admin_only"] else "— выберите участника —")
                     if i == NONE else names.get(i, "?"))
    owner_id = ss.get(f("owner")) if is_admin else acting["id"]

    for key, label, ftype, required, extra in ach.FORMS[form]:
        lab = label + (" *" if required and ftype not in ("check",) else "")
        hlp = extra.get("help")
        if ftype == "text":
            ss.setdefault(f(key), "")
            st.text_input(lab, key=f(key), help=hlp,
                          max_chars=ach.NUMBER_MAX_LEN if key == "number" else None)
        elif ftype == "textarea":
            ss.setdefault(f(key), "")
            st.text_area(lab, key=f(key), help=hlp, height=90)
        elif ftype == "date":
            ss.setdefault(f(key), date.today())
            st.date_input(lab, key=f(key), format="DD.MM.YYYY")
        elif ftype == "date_opt":
            ss.setdefault(f(key), None)
            st.date_input(lab, key=f(key), format="DD.MM.YYYY")
        elif ftype == "select":
            if ss.get(f(key)) not in extra["options"]:
                ss[f(key)] = extra["options"][0]
            st.selectbox(lab, extra["options"], key=f(key))
        elif ftype == "check":
            ss.setdefault(f(key), extra.get("default", False))
            st.checkbox(label, key=f(key), help=hlp)
        elif ftype == "year":
            ss.setdefault(f(key), date.today().year)
            st.number_input(lab, min_value=1990, max_value=2100, step=1, key=f(key))
        elif ftype == "ayear":
            ay_opts = ach.academic_years()
            cur = ss.get(f(key))
            if cur and cur not in ay_opts:
                ay_opts = [cur, *ay_opts]
            if cur not in ay_opts:
                today = date.today()
                ss[f(key)] = f"{today.year}-{today.year + 1}" if today.month >= 9 else f"{today.year - 1}-{today.year}"
            st.selectbox(lab, ay_opts, key=f(key))
        elif ftype == "meeting":
            if not is_admin:
                continue
            ms = db.list_meetings()
            mopts = {m["id"]: f"{m['meeting_date'].strftime('%d.%m.%Y')} — {m['kind']}: {m['topic'] or '(без темы)'}"
                     for m in reversed(ms)}
            if ss.get(f(key)) not in (NONE, *mopts):
                ss[f(key)] = NONE
            st.selectbox(label, [NONE, *mopts], key=f(key), on_change=_meeting_prefill_cb, args=(pfx,),
                         format_func=lambda i: "— не связывать —" if i == NONE else mopts[i])
        elif ftype == "people":
            people_opts = [u for u in names if u != owner_id]
            ss[f("members")] = [u for u in ss.get(f("members"), []) if u in people_opts]
            st.multiselect(("Другие участники из СНО" if not kind["admin_only"] else label + " из СНО"),
                           people_opts, key=f("members"), format_func=lambda i: names.get(i, "?"),
                           placeholder="Выберите участников СНО")
            ss.setdefault(f("externals"), "")
            st.text_input("Внешние участники (текстом, через запятую)", key=f("externals"))
        elif ftype == "authors":
            _authors_block(pfx, names, owner_id, acting, is_admin)

    if form == "publication":
        title = (ss.get(f("title")) or "").strip()
        if title or ss.get(f("doi")):
            dup = ach.find_duplicate_publication(title, ss.get(f("year")), ss.get(f("doi")),
                                                 exclude_id=record["id"] if record else None)
            if dup:
                st.warning(f"Похоже, эта статья уже внесена: «{dup['title']}» — запись участника "
                           f"{dup['owner_name'] or 'СНО'}. Статья считается один раз: попросите его "
                           f"добавить вас соавтором.")
    if form == "doklad" and not ss.get(f("ochno"), True):
        st.caption("Заочный доклад сохранится, но в годовой отчёт не попадёт.")
    st.button("Сохранить изменения" if record else "Сохранить", type="primary", key=_k(pfx, "save"),
              on_click=_save_cb, args=(pfx, acting, record["id"] if record else None, form))
    show_msgs(_k(pfx, "msg"))


def _authors_block(pfx: str, names: dict, owner_id: Optional[int], acting: dict, is_admin: bool) -> None:
    ss = st.session_state
    f = lambda n: _k(pfx, "f_" + n)  # noqa: E731
    st.markdown("**Авторы и доли участия** *")
    main_name = names.get(owner_id) if owner_id else None
    c1, c2 = st.columns([3, 1])
    c1.markdown(f"Главный автор (владелец записи): **{main_name or '— выберите выше —'}**")
    ss.setdefault(f("owner_share"), None)
    c2.number_input("Доля, % *", min_value=0.0, max_value=100.0, step=1.0, key=f("owner_share"),
                    format="%g")
    ss.setdefault(f("n_co"), 0)
    n = int(st.number_input("Соавторов", min_value=0, max_value=30, step=1, key=f("n_co"),
                            help="Соавторы вносятся строками: участник СНО из списка или ФИО текстом, "
                                 "и доля участия. Соавторы не получают отдельную запись."))
    member_opts = [NONE, *[u for u in names if u != owner_id]]
    total = ss.get(f("owner_share")) or 0
    for i in range(n):
        a, b, c = st.columns([2, 2, 1])
        if ss.get(f(f"co_user_{i}")) not in member_opts:
            ss[f(f"co_user_{i}")] = NONE
        uid = a.selectbox(f"Соавтор {i + 1}: участник СНО", member_opts, key=f(f"co_user_{i}"),
                          format_func=lambda x: EXTERNAL if x == NONE else names.get(x, "?"))
        ss.setdefault(f(f"co_name_{i}"), "")
        b.text_input(f"Соавтор {i + 1}: ФИО (если не из СНО)", key=f(f"co_name_{i}"), disabled=uid != NONE)
        ss.setdefault(f(f"co_share_{i}"), None)
        c.number_input(f"Доля {i + 1}, % *", min_value=0.0, max_value=100.0, step=1.0,
                       key=f(f"co_share_{i}"), format="%g")
        total += ss.get(f(f"co_share_{i}")) or 0
    ss.setdefault(f("main_pos"), 1)
    if n:
        if ss.get(f("main_pos"), 1) > n + 1:
            ss[f("main_pos")] = n + 1
        st.number_input("Место главного автора в списке авторов", min_value=1, max_value=n + 1, step=1,
                        key=f("main_pos"), help="Порядок авторов и долей в строке отчёта")
    msg = f"Сумма долей: {total:g}%"
    if total > 100:
        st.error(msg + " — больше 100%, сохранение невозможно.")
    else:
        st.caption(msg)


# ── Lists ───────────────────────────────────────────────────────────────────


def _rec_caption(r: dict) -> str:
    parts = []
    if r["subpoint_label"]:
        parts.append(r["subpoint_label"])
    if r["row_label"]:
        parts.append(ach.strip_dash(r["row_label"]))
    if r["form"] == "doklad" and r["topic"]:
        parts.append(f"тема: «{r['topic']}»")
    if r["form"] == "doklad" and not r["ochno"]:
        parts.append("заочно — не в отчёте")
    if r["form"] == "publication":
        parts.append(ach.shares_text(r))
        others = [p["name"] for p in r["people"] if p["name"]]
        if others:
            parts.append("соавторы: " + ", ".join(others))
    elif r["people"] or r["externals"]:
        parts.append("участники: " + ", ".join(ach.people_names(r, with_owner=False)))
    return " · ".join(parts)


def member_list(user: dict) -> None:
    """«Мои достижения»: grouped by kind, edit / delete own records."""
    recs = ach.list_achievements(owner_id=user["id"])
    st.subheader(f"Мои достижения ({len(recs)})")
    show_msgs("mine_msg")
    if not recs:
        st.info("Пока нет записей. Добавьте первую выше.")
        return
    todo = [r for r in recs if r["issues"]]
    if todo:
        st.warning(f"Требуют заполнения: {len(todo)} — отмечены значком «⚠ заполните». "
                   "Без уровня/подпункта запись не попадёт в годовой отчёт.")
    groups: dict[str, list[dict]] = {}
    for r in recs:
        groups.setdefault(r["kind_label"], []).append(r)
    for label, items in groups.items():
        st.markdown(f"##### {label} ({len(items)})")
        for r in items:
            _record_row(r, user, owner_only=True)


def _record_row(r: dict, acting: dict, owner_only: bool) -> None:
    rid = r["id"]
    c1, c2, c3, c4, c5 = st.columns([5, 2, 2, 1, 1], vertical_alignment="center")
    title = r["title"] or "(без названия)"
    c1.markdown(f"**{title}**" + ("  \n:orange-badge[⚠ заполните: " + ", ".join(r["issues"]) + "]"
                                   if r["issues"] else ""))
    cap = _rec_caption(r)
    if cap:
        c1.caption(cap)
    c2.write(ach.fmt_date(r["date_from"]) + (f"–{ach.fmt_date(r['date_to'])}" if r["date_to"] and r["form"] != "publication" else ""))
    c3.caption(f"№ {r['number']}" if r["number"] else "без номера")
    edit_key = f"edit_{rid}"
    if c4.button("✏️", key=f"btn_edit_{rid}", help="Изменить"):
        st.session_state[edit_key] = not st.session_state.get(edit_key, False)
        reset_form(f"e{rid}_")
    if c5.button("🗑", key=f"btn_del_{rid}", help="Удалить"):
        st.session_state[f"confirm_del_a_{rid}"] = True
    if st.session_state.get(f"confirm_del_a_{rid}"):
        st.warning(f"Удалить «{title}»?")
        b1, b2, _ = st.columns([1, 1, 4])
        if b1.button("Да, удалить", key=f"yes_a_{rid}", type="primary"):
            ach.delete_achievement(rid, owner_id=acting["id"] if owner_only else None)
            st.session_state.pop(f"confirm_del_a_{rid}", None)
            st.session_state["mine_msg"] = ("success", "Запись удалена.")
            st.rerun()
        if b2.button("Отмена", key=f"no_a_{rid}"):
            st.session_state.pop(f"confirm_del_a_{rid}", None)
            st.rerun()
    if st.session_state.get(edit_key):
        with st.container(border=True):
            achievement_form(f"e{rid}_", acting, record=r)


def year_summary_text(user: dict, year: int) -> tuple[bool, str]:
    items = ach.member_year_summary(user["id"], year)
    if not items:
        return False, f"В {year} году у тебя пока нет записей — самое время добавить первое достижение! 🚀"
    total = sum(n for _, n in items)
    return True, (f"В {year} году у тебя {ru_text.with_count(total, RECORD_FORMS)}: "
                  + ", ".join(f"{k.lower()} — {n}" for k, n in items) + ".")


# ── Admin: all achievements ─────────────────────────────────────────────────


def admin_achievements(acting: dict, year_choices: list[int], year_label) -> None:  # noqa: ANN001
    import pandas as pd

    st.subheader("Все достижения")
    with st.expander("➕ Добавить за участника (или от СНО)"):
        achievement_form("ad_", acting, on_behalf=True)
    show_msgs("adm_a_msg")

    users = db.list_users(active_only=False)
    uopts = {0: "— все —", **{u["id"]: f"{u['full_name']} (@{u['login']})" for u in users}}
    cat = ach.catalog()
    iopts = {0: "— все —", **{i["id"]: i["label"].strip()[:70] for i in cat}}
    kinds = ach.list_kinds(admin=True, include_hidden=True)
    kopts = {0: "— все —", **{k["id"]: k["label"] for k in kinds}}

    f1, f2, f3, f4 = st.columns([3, 2, 3, 2])
    uid = f1.selectbox("Участник", list(uopts), format_func=uopts.get, key="aa_user")
    kid = f2.selectbox("Вид", list(kopts), format_func=kopts.get, key="aa_kind")
    iid = f3.selectbox("Показатель", list(iopts), format_func=iopts.get, key="aa_ind")
    year = f4.selectbox("Период", year_choices, format_func=year_label, key="aa_year")
    g1, g2, g3 = st.columns([3, 2, 2])
    rid = 0
    if iid:
        rows = {0: "— все —", **{r["id"]: ach.strip_dash(r["label"])[:60]
                                 for r in next(i for i in cat if i["id"] == iid)["rows"]}}
        if len(rows) > 1:
            if st.session_state.get("aa_row") not in rows:
                st.session_state["aa_row"] = 0
            rid = g1.selectbox("Уровень / подпункт", list(rows), format_func=rows.get, key="aa_row")
    no_num = g2.checkbox("Без номера достижения", key="aa_nonum")
    todo = g3.checkbox("Требуют заполнения", key="aa_todo")
    recs = ach.list_achievements(owner_id=uid or None, year=year or None, indicator_id=iid or None,
                                 row_id=rid or None, kind_id=kid or None, without_number=no_num, needs_fill=todo)
    st.caption(f"Найдено: {len(recs)}")
    if not recs:
        st.info("Нет записей по выбранным фильтрам.")
        return
    st.dataframe(
        pd.DataFrame([{
            "ФИО": r["owner_name"] or "СНО", "Вид": r["kind_label"],
            "Уровень / подпункт": ach.strip_dash(r["row_label"] or r["subpoint_label"] or ""),
            "Название": r["title"], "Дата": ach.fmt_date(r["date_from"]),
            "Номер в портфолио": r["number"] or "", "Есть номер": bool(r["number"]),
            "В отчёте": bool(r["counted"]), "Заполните": ", ".join(r["issues"]),
        } for r in recs]),
        hide_index=True, width="stretch",
        column_config={
            "Номер в портфолио": st.column_config.TextColumn("Номер в портфолио"),
            "Есть номер": st.column_config.CheckboxColumn("Есть номер", disabled=True),
            "В отчёте": st.column_config.CheckboxColumn("В отчёте", disabled=True),
        },
    )
    st.markdown("#### Редактирование и удаление")
    ids = {r["id"]: f"{r['owner_name'] or 'СНО'} — {r['kind_label']}: {r['title']} ({ach.fmt_date(r['date_from'])})"
           for r in recs}
    if st.session_state.get("aa_pick") not in (0, *ids):
        st.session_state["aa_pick"] = 0
    pick = st.selectbox("Выберите запись", [0, *ids], key="aa_pick",
                        format_func=lambda x: "—" if x == 0 else ids[x])
    if not pick:
        return
    r = next(x for x in recs if x["id"] == pick)
    with st.container(border=True):
        achievement_form(f"ae{pick}_", acting, record=r)
    if st.button("🗑 Удалить запись", key=f"aa_del_{pick}"):
        st.session_state["aa_confirm"] = pick
    if st.session_state.get("aa_confirm") == pick:
        st.warning(f"Удалить запись «{r['title']}» ({r['owner_name'] or 'СНО'})?")
        b1, b2, _ = st.columns([1, 1, 4])
        if b1.button("Да, удалить", type="primary", key="aa_yes"):
            ach.delete_achievement(pick)
            st.session_state.pop("aa_confirm", None)
            st.session_state["adm_a_msg"] = ("success", "Запись удалена.")
            st.rerun()
        if b2.button("Отмена", key="aa_no"):
            st.session_state.pop("aa_confirm", None)
            st.rerun()


# ── Admin: catalog editor («Настройки») ─────────────────────────────────────


def catalog_editor() -> None:
    import pandas as pd

    st.subheader("Показатели годового отчёта")
    st.caption("Формулировки идут в отчёт дословно. Показатель или подпункт с записями нельзя удалить — "
               "только скрыть (скрытый не предлагается при добавлении и не выводится в отчёт, "
               "если по нему нет записей).")
    show_msgs("cat_msg")
    cat = ach.catalog()
    st.dataframe(pd.DataFrame([{"№": n, "Показатель": i["label"].strip(), "Подпунктов": len(i["rows"]),
                                "Записей": i["records"] + sum(r["records"] for r in i["rows"]),
                                "Скрыт": bool(i["hidden"])} for n, i in enumerate(cat, start=1)]),
                 hide_index=True, width="stretch", height=280,
                 column_config={"Скрыт": st.column_config.CheckboxColumn("Скрыт", disabled=True)})
    opts = {i["id"]: f"{n}. {i['label'].strip()[:90]}" for n, i in enumerate(cat, start=1)}
    if st.session_state.get("cat_pick") not in opts:
        st.session_state["cat_pick"] = next(iter(opts))
    iid = st.selectbox("Показатель для редактирования", list(opts), format_func=opts.get, key="cat_pick")
    ind = next(i for i in cat if i["id"] == iid)
    with st.container(border=True):
        label = st.text_area("Формулировка показателя", value=ind["label"], key=f"cat_label_{iid}", height=80)
        hidden = st.checkbox("Скрыть показатель", value=bool(ind["hidden"]), key=f"cat_hidden_{iid}")
        c1, c2, c3, c4 = st.columns(4)
        if c1.button("Сохранить показатель", type="primary", key=f"cat_save_{iid}"):
            try:
                ach.update_indicator(iid, label, hidden)
                st.session_state["cat_msg"] = ("success", "Показатель сохранён.")
            except ValueError as e:
                st.session_state["cat_msg"] = ("error", str(e))
            st.rerun()
        if c2.button("↑ Выше", key=f"cat_up_{iid}"):
            ach.move_indicator(iid, -1)
            st.rerun()
        if c3.button("↓ Ниже", key=f"cat_down_{iid}"):
            ach.move_indicator(iid, +1)
            st.rerun()
        if c4.button("Удалить", key=f"cat_del_{iid}"):
            try:
                ach.delete_indicator(iid)
                st.session_state["cat_msg"] = ("success", "Показатель удалён.")
                st.session_state.pop("cat_pick", None)
            except ValueError as e:
                st.session_state["cat_msg"] = ("error", str(e))
            st.rerun()

        st.markdown("**Подпункты** (порядок — колонка «Порядок»)")
        if ind["rows"]:
            df = pd.DataFrame([{"id": r["id"], "Порядок": n, "Формулировка": r["label"], "Скрыт": bool(r["hidden"]),
                                "Записей": r["records"]} for n, r in enumerate(ind["rows"], start=1)])
            edited = st.data_editor(df, hide_index=True, width="stretch", key=f"cat_rows_{iid}",
                                    disabled=["id", "Записей"], column_config={
                                        "id": None,
                                        "Порядок": st.column_config.NumberColumn("Порядок", min_value=1, step=1, width="small"),
                                        "Формулировка": st.column_config.TextColumn("Формулировка", width="large"),
                                        "Скрыт": st.column_config.CheckboxColumn("Скрыт")})
            if st.button("Сохранить подпункты", key=f"cat_rows_save_{iid}"):
                try:
                    for rec in edited.to_dict("records"):
                        ach.update_row(int(rec["id"]), rec["Формулировка"], bool(rec["Скрыт"]),
                                       sort_order=int(rec["Порядок"] or 0) * 10)
                    st.session_state["cat_msg"] = ("success", "Подпункты сохранены.")
                except ValueError as e:
                    st.session_state["cat_msg"] = ("error", str(e))
                st.rerun()
            empties = {r["id"]: ach.strip_dash(r["label"])[:60] for r in ind["rows"] if not r["records"]}
            if empties:
                d1, d2 = st.columns([3, 1], vertical_alignment="bottom")
                rdel = d1.selectbox("Удалить подпункт без записей", [0, *empties], key=f"cat_rdel_{iid}",
                                    format_func=lambda x: "—" if x == 0 else empties[x])
                if d2.button("Удалить подпункт", key=f"cat_rdel_btn_{iid}", disabled=not rdel):
                    try:
                        ach.delete_row(rdel)
                        st.session_state["cat_msg"] = ("success", "Подпункт удалён.")
                    except ValueError as e:
                        st.session_state["cat_msg"] = ("error", str(e))
                    st.rerun()
        else:
            st.caption("Без подпунктов — количество и детали пишутся в строке показателя.")
        a1, a2 = st.columns([3, 1], vertical_alignment="bottom")
        new_row = a1.text_input("Новый подпункт", key=f"cat_newrow_{iid}", placeholder="- городской уровень")
        if a2.button("Добавить подпункт", key=f"cat_addrow_{iid}"):
            try:
                ach.add_row(iid, new_row)
                st.session_state["cat_msg"] = ("success", "Подпункт добавлен.")
            except ValueError as e:
                st.session_state["cat_msg"] = ("error", str(e))
            st.rerun()

    with st.expander("➕ Новый показатель"):
        nl = st.text_input("Формулировка (с номером, как в форме)", key="cat_new_label",
                           placeholder="19. …")
        dim = st.radio("Подпункты", ["level", "subtype", "none"], horizontal=True, key="cat_new_dim",
                       format_func={"level": "уровни", "subtype": "подвиды", "none": "без подпунктов"}.get)
        if st.button("Добавить показатель", key="cat_new_btn"):
            try:
                new_id = ach.add_indicator(nl, dim)
                if dim == "level":
                    for lab in ("- международный уровень", "- всероссийский уровень", "- региональный уровень",
                                "- внутривузовский уровень (без статуса)"):
                        ach.add_row(new_id, lab)
                st.session_state["cat_msg"] = ("success", "Показатель добавлен (и вид для участников с тем же названием).")
                st.session_state["cat_pick"] = new_id
            except ValueError as e:
                st.session_state["cat_msg"] = ("error", str(e))
            st.rerun()

    with st.expander("Виды достижений (что видит участник)"):
        kinds = ach.list_kinds(admin=True, include_hidden=True)
        kdf = pd.DataFrame([{"id": k["id"], "Вид": k["label"], "Скрыт": bool(k["hidden"]),
                             "Только админ": bool(k["admin_only"]),
                             "Показатель": next((f"п.{n}" for n, i in enumerate(cat, 1) if i["id"] == k["indicator_id"]),
                                                "подпункты" if k["subpoints"] else "")} for k in kinds])
        ked = st.data_editor(kdf, hide_index=True, width="stretch", key="cat_kinds",
                             disabled=["id", "Только админ", "Показатель"], column_config={"id": None})
        if st.button("Сохранить виды", key="cat_kinds_save"):
            try:
                for rec in ked.to_dict("records"):
                    ach.rename_kind(int(rec["id"]), rec["Вид"], bool(rec["Скрыт"]))
                st.session_state["cat_msg"] = ("success", "Виды сохранены.")
            except ValueError as e:
                st.session_state["cat_msg"] = ("error", str(e))
            st.rerun()


# ── Admin: annual report section («Отчёт») ──────────────────────────────────


def annual_report_section(year_choices: list[int]) -> None:
    import pandas as pd

    import annual_report

    st.subheader("Годовой отчёт о работе СНО")
    year = st.selectbox("Год", year_choices, key="annual_year")
    settings = db.get_report_settings()
    data = ach.report_data(year)
    st.caption(f"Заголовок: «Отчет о работе СНО «{settings['sno_name']}»», «За {year} год». "
               "Альбомная страница, 18 показателей в форме образца. Подписанты — во вкладке «Настройки».")

    c1, c2, c3 = st.columns(3)
    c1.metric("В отчёте (единиц)", data["total"])
    c2.metric("Требуют заполнения", len(data["warnings"]))
    c3.metric("Заочных докладов (не в отчёте)", data["zaochno"])

    if data["warnings"]:
        with st.expander(f"⚠ Предупреждения ({len(data['warnings'])}) — записи без уровня/подпункта/долей",
                         expanded=True):
            st.caption("Записи без уровня или подпункта не попадают в отчёт, пока их не заполнят "
                       "(участник в «Мои достижения» или админ во вкладке «Все достижения»).")
            st.dataframe(pd.DataFrame([{
                "Участник": w["owner"], "Вид": w["kind"], "Название": w["title"], "Дата": w["date"],
                "Заполните": w["issues"], "В отчёте": bool(w["counted"])} for w in data["warnings"]]),
                hide_index=True, width="stretch",
                column_config={"В отчёте": st.column_config.CheckboxColumn("В отчёте", disabled=True)})
    else:
        st.success("Все записи за год заполнены.")

    with st.expander("Предпросмотр: количество по показателям"):
        prev = []
        for n, ind in enumerate(data["indicators"], start=1):
            prev.append({"Показатель": ind["label"].strip(), "Количество": str(ind["count"])})
            for row in ind["rows"]:
                prev.append({"Показатель": "      " + row["label"], "Количество": str(row["count"] or "")})
        st.dataframe(pd.DataFrame(prev), hide_index=True, width="stretch", height=420)

    key = f"annual_concl_{year}"
    if key not in st.session_state:
        st.session_state[key] = ach.get_conclusion(year)
    st.text_area("Заключение (абзац после таблиц)", key=key, height=150,
                 placeholder=f"Деятельность СНО «{settings['sno_name']}» в {year} году …")
    if st.button("Сохранить заключение", key=f"annual_concl_save_{year}"):
        ach.set_conclusion(year, st.session_state[key])
        st.success("Заключение сохранено.")

    sigs = [s for s in settings["signatories"] if s.get("position") or s.get("name")]
    if sigs:
        st.caption("Подписи: " + "; ".join(f"{s['position']} ___/ {s['name']}" for s in sigs))
    try:
        docx_bytes = annual_report.build_annual_docx(data, st.session_state.get(key, ""), settings["signatories"])
    except Exception as exc:  # noqa: BLE001
        st.error(f"Не удалось сформировать отчёт: {exc.__class__.__name__}: {exc}")
        docx_bytes = None
    d1, d2 = st.columns(2)
    if docx_bytes:
        d1.download_button("⬇️ Годовой отчёт (.docx)", data=docx_bytes,
                           file_name=f"Отчет_о_работе_СНО_{year}.docx",
                           mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                           type="primary", key="dl_annual_docx")
    d2.download_button("⬇️ Достижения за год (.xlsx)",
                       data=annual_report.build_achievements_xlsx(data, db.list_meetings(year)),
                       file_name=f"Достижения_СНО_{year}.xlsx",
                       mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                       key="dl_annual_xlsx")


# ── Stats ───────────────────────────────────────────────────────────────────


def _counts_by_indicator(year: Optional[int]) -> list[dict]:
    recs = ach.list_achievements(year=year)
    cat = ach.catalog()
    out = []
    for n, ind in enumerate(cat, start=1):
        rr = [r for r in recs if r["indicator_id"] == ind["id"]]
        if ind["hidden"] and not rr:
            continue
        out.append({"№": n, "Показатель": re.sub(r"^\d+\.\s*", "", ind["label"].strip())[:60],
                    "Записей": len(rr), "В отчёте": sum(1 for r in rr if r["counted"])})
    return out


def member_stats(year_choices: list[int], year_label) -> None:  # noqa: ANN001
    """СНО-wide statistics for members: no names, logins or numbers."""
    import pandas as pd
    import plotly.express as px

    st.subheader("Статистика СНО")
    year = st.selectbox("Период", year_choices, index=year_choices.index(date.today().year),
                        format_func=year_label, key="m_stats_year")
    y = year or None
    events = ach.events_overview(y)
    c1, c2 = st.columns(2)
    c1.metric("Мероприятий и работ", len(events))
    c2.metric("Достижений", sum(e["records"] for e in events))
    if not events:
        st.info("За выбранный период записей пока нет.")
        return
    kinds = [k["label"] for k in ach.list_kinds(admin=True)]
    counts = {k: {"ev": 0, "rec": 0} for k in kinds}
    for e in events:
        c = counts.setdefault(e["kind"], {"ev": 0, "rec": 0})
        c["ev"] += 1
        c["rec"] += e["records"]
    df = pd.DataFrame([{"Вид": k, "Мероприятий": v["ev"], "Достижений": v["rec"]} for k, v in counts.items()
                       if v["rec"] or k in kinds[:6]])
    st.markdown("#### По видам")
    g1, g2 = st.columns([3, 2])
    with g1:
        fig = px.bar(df, x="Вид", y="Достижений", text="Достижений")
        fig.update_layout(height=300, margin=dict(t=10, b=10, l=10, r=10), xaxis_title=None, yaxis_title=None,
                          yaxis=dict(fixedrange=True, rangemode="tozero"), xaxis=dict(fixedrange=True))
        fig.update_traces(textposition="outside", hovertemplate="%{x}: %{y}<extra></extra>")
        st.plotly_chart(fig, width="stretch", config={"displayModeBar": False}, key="m_stats_chart")
    with g2:
        st.dataframe(df, hide_index=True, width="stretch")
    st.markdown("#### Мероприятия и работы")
    sel = st.multiselect("Вид", sorted({e["kind"] for e in events}), key="m_stats_types", placeholder="Все виды")
    rows = [e for e in events if not sel or e["kind"] in sel]
    st.dataframe(pd.DataFrame([{"Название": e["title"], "Вид": e["kind"], "Дата": ach.fmt_date(e["date"]),
                                "Участников": e["people"]} for e in rows],
                              columns=["Название", "Вид", "Дата", "Участников"]),
                 hide_index=True, width="stretch")
