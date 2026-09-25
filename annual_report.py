"""Годовой «Отчет о работе СНО «…» за YYYY год» (.docx) и выгрузка достижений в Excel.

Шаблон templates/sno_annual_template.docx собран из образца формы (только оформление):
альбомная страница, шрифт Times New Roman, 2 таблицы «Показатель | Количество | Отметка…»
с ширинами колонок образца. В шаблоне есть строки-прототипы (показатель — жирный,
подпункт) и абзацы-прототипы деталей (строка мероприятия, нумерованная строка с ФИО
курсивом, нумерованная строка). Нумерация деталей начинается с 1 в каждом подпункте.
"""

from __future__ import annotations

import copy
import io
from pathlib import Path
from typing import Optional

from docx import Document
from docx.oxml.ns import qn

TEMPLATE_PATH = Path(__file__).resolve().parent / "templates" / "sno_annual_template.docx"
SIGNATURE_LINE = "_______________"
FIRST_TABLE_INDICATORS = 5  # как в образце: п.1–5 в первой таблице, остальные во второй


def _set_text(p, value: str) -> None:  # noqa: ANN001
    """Replace paragraph text, keeping the first run's formatting."""
    runs = p.findall(qn("w:r"))
    for r in runs[1:]:
        p.remove(r)
    if not runs:
        return
    r = runs[0]
    for el in list(r):
        if el.tag in (qn("w:t"), qn("w:lastRenderedPageBreak"), qn("w:br"), qn("w:tab")):
            r.remove(el)
    t = r.makeelement(qn("w:t"), {})
    t.text = value
    t.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
    r.append(t)


def _run_text(r, value: str) -> None:  # noqa: ANN001
    for el in r.findall(qn("w:t")):
        el.text = value
        el.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")


class _Numbering:
    """New w:num per sub-row (same abstract list as the template) → numbering restarts at 1."""

    def __init__(self, doc, proto_num_id: str) -> None:  # noqa: ANN001
        self.el = doc.part.numbering_part.element
        src = next(n for n in self.el.findall(qn("w:num")) if n.get(qn("w:numId")) == proto_num_id)
        self.abstract = src.find(qn("w:abstractNumId")).get(qn("w:val"))
        self.next_id = max(int(n.get(qn("w:numId"))) for n in self.el.findall(qn("w:num"))) + 1

    def new(self) -> str:
        num = self.el.makeelement(qn("w:num"), {qn("w:numId"): str(self.next_id)})
        a = num.makeelement(qn("w:abstractNumId"), {qn("w:val"): self.abstract})
        num.append(a)
        ov = num.makeelement(qn("w:lvlOverride"), {qn("w:ilvl"): "0"})
        so = ov.makeelement(qn("w:startOverride"), {qn("w:val"): "1"})
        ov.append(so)
        num.append(ov)
        self.el.append(num)
        self.next_id += 1
        return str(self.next_id - 1)


def _num_id(p) -> Optional[str]:  # noqa: ANN001
    n = p.find(qn("w:pPr") + "/" + qn("w:numPr") + "/" + qn("w:numId"))
    return n.get(qn("w:val")) if n is not None else None


def _set_num(p, num_id: str) -> None:  # noqa: ANN001
    p.find(qn("w:pPr") + "/" + qn("w:numPr") + "/" + qn("w:numId")).set(qn("w:val"), num_id)


def build_annual_docx(data: dict, conclusion: str, signatories: list[dict],
                      template: Path = TEMPLATE_PATH) -> bytes:
    """data = achievements.report_data(year)."""
    doc = Document(str(template))
    body = doc.element.body
    paras = [c for c in body if c.tag == qn("w:p")]
    _set_text(paras[0], f"Отчет о работе СНО «{data['sno_name']}»")
    _set_text(paras[1], f"За {data['year']} год")

    t0, t1 = doc.tables
    trs = t0._tbl.findall(qn("w:tr"))
    proto_ind, proto_sub = trs[1], trs[2]
    t0._tbl.remove(proto_ind)
    t0._tbl.remove(proto_sub)
    sub_tcs = proto_sub.findall(qn("w:tc"))
    detail_protos = sub_tcs[2].findall(qn("w:p"))
    p_event, p_name, p_line = detail_protos
    empty_detail = copy.deepcopy(p_event)
    _set_text(empty_detail, "")
    for p in detail_protos:
        sub_tcs[2].remove(p)
    numbering = _Numbering(doc, _num_id(p_name))

    def fill_details(tc, items: list[tuple]) -> None:  # noqa: ANN001
        for p in tc.findall(qn("w:p")):
            tc.remove(p)
        if not items:
            tc.append(copy.deepcopy(empty_detail))
            return
        num_id = numbering.new()
        for it in items:
            if it[0] == "event":
                p = copy.deepcopy(p_event)
                _set_text(p, it[1])
            elif it[0] == "name":
                p = copy.deepcopy(p_name)
                r1, r2 = p.findall(qn("w:r"))[:2]
                _run_text(r1, it[1])
                _run_text(r2, it[2])
                _set_num(p, num_id)
            else:
                p = copy.deepcopy(p_line)
                _set_text(p, it[1])
                _set_num(p, num_id)
            tc.append(p)

    def add_row(tbl, proto, label: str, count: str, items: list[tuple]) -> None:  # noqa: ANN001
        tr = copy.deepcopy(proto)
        tcs = tr.findall(qn("w:tc"))
        _set_text(tcs[0].find(qn("w:p")), label)
        _set_text(tcs[1].find(qn("w:p")), count)
        if items or proto is proto_sub:
            fill_details(tcs[2], items)
        tbl.append(tr)

    for n, ind in enumerate(data["indicators"]):
        tbl = t0._tbl if n < FIRST_TABLE_INDICATORS else t1._tbl
        # indicator row: total (0 when empty, as in the example); details only for
        # indicators without sub-rows (17, 18)
        add_row(tbl, proto_ind, ind["label"], str(ind["count"]), ind["items"])
        for row in ind["rows"]:
            add_row(tbl, proto_sub, row["label"], str(row["count"]) if row["count"] else "", row["items"])

    # conclusion + signatures
    _set_text(paras[6], conclusion or "")
    sig_proto, empty_after = paras[8], paras[9]
    anchor = empty_after
    lines = [s for s in signatories if s.get("position") or s.get("name")]
    if lines:
        _set_text(sig_proto, f"{lines[0]['position']} {SIGNATURE_LINE}/ {lines[0]['name']}".strip())
        for s in lines[1:]:
            p = copy.deepcopy(sig_proto)
            _set_text(p, f"{s['position']} {SIGNATURE_LINE}/ {s['name']}".strip())
            e = copy.deepcopy(empty_after)
            anchor.addnext(p)
            p.addnext(e)
            anchor = e
    else:
        _set_text(sig_proto, "")
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def build_achievements_xlsx(data: dict, meetings: Optional[list[dict]] = None) -> bytes:
    """Excel: «Достижения» (все записи года), «Показатели» (счётчики как в отчёте),
    «Заседания» (если переданы)."""
    import pandas as pd

    import achievements as ach

    recs = data["records"]
    rows = []
    for r in recs:
        rows.append({
            "Владелец": r["owner_name"] or "СНО",
            "Логин": r["owner_login"] or "",
            "Вид": r["kind_label"],
            "Подпункт гранта": r["subpoint_label"] or "",
            "Показатель": r["indicator_label"] or "",
            "Уровень / подпункт": ach.strip_dash(r["row_label"] or ""),
            "Название": r["title"],
            "Тема": r["topic"] or "",
            "Дата с": ach.fmt_date(r["date_from"]),
            "Дата по": ach.fmt_date(r["date_to"]),
            "Участники / соавторы": ", ".join(ach.people_names(r, with_owner=False)),
            "Доли участия": ach.shares_text(r) if r["form"] == "publication" else "",
            "Очно": ("да" if r["ochno"] else "нет") if r["form"] == "doklad" else "",
            "Номер в портфолио": r["number"] or "",
            "Есть номер": "да" if r["number"] else "нет",
            "Ссылка": r["link"] or "",
            "В отчёте": "да" if r["counted"] else "нет",
            "Заполните": ", ".join(r["issues"]),
            "Строка отчёта": (ach.record_line(r, data["sno_name"]) if r["form"] != "doklad"
                              else f"{r['owner_name']} доклад на тему: «{r['topic'] or ''}»"),
        })
    cols = ["Владелец", "Логин", "Вид", "Подпункт гранта", "Показатель", "Уровень / подпункт", "Название",
            "Тема", "Дата с", "Дата по", "Участники / соавторы", "Доли участия", "Очно", "Номер в портфолио",
            "Есть номер", "Ссылка", "В отчёте", "Заполните", "Строка отчёта"]
    counts = []
    for ind in data["indicators"]:
        counts.append({"Показатель / подпункт": ind["label"].strip(), "Количество": ind["count"]})
        for row in ind["rows"]:
            counts.append({"Показатель / подпункт": "    " + row["label"], "Количество": row["count"]})
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xw:
        pd.DataFrame(rows, columns=cols).to_excel(xw, sheet_name="Достижения", index=False)
        pd.DataFrame(counts, columns=["Показатель / подпункт", "Количество"]).to_excel(
            xw, sheet_name="Показатели", index=False)
        sheets = ["Достижения", "Показатели"]
        if meetings is not None:
            pd.DataFrame([{"№": i, "Вид": m.get("kind") or "",
                           "Дата": ach.fmt_date(m["meeting_date"]), "Время": m.get("meeting_time") or "",
                           "Локация": m.get("location") or "", "Тема": m.get("topic") or "",
                           "Формат": m.get("format") or ""} for i, m in enumerate(meetings, start=1)],
                         columns=["№", "Вид", "Дата", "Время", "Локация", "Тема", "Формат"]).to_excel(
                xw, sheet_name="Заседания", index=False)
            sheets.append("Заседания")
        for name in sheets:
            ws = xw.sheets[name]
            for col in ws.columns:
                width = max((len(str(c.value)) if c.value is not None else 0) for c in col)
                ws.column_dimensions[col[0].column_letter].width = min(max(10, width + 2), 70)
    return buf.getvalue()
