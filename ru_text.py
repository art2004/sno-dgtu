"""Русские формы слов: склонение по числу и короткие названия месяцев."""

from __future__ import annotations

MONTHS_SHORT = ("янв", "фев", "мар", "апр", "май", "июн",
                "июл", "авг", "сен", "окт", "ноя", "дек")

# тип мероприятия → (1, 2–4, 5+)
EVENT_TYPE_FORMS = {
    "конференция": ("конференция", "конференции", "конференций"),
    "грант": ("грант", "гранта", "грантов"),
    "конкурс": ("конкурс", "конкурса", "конкурсов"),
    "стипендия": ("стипендия", "стипендии", "стипендий"),
    "статья": ("статья", "статьи", "статей"),
}
SUMMARY_ORDER = ("конференция", "грант", "конкурс", "стипендия", "статья")
PARTICIPATION_FORMS = ("участие", "участия", "участий")


def plural(n: int, forms: tuple[str, str, str]) -> str:
    """plural(1, ('участие','участия','участий')) → 'участие'; 3 → 'участия'; 11 → 'участий'."""
    n = abs(int(n))
    if n % 10 == 1 and n % 100 != 11:
        return forms[0]
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return forms[1]
    return forms[2]


def with_count(n: int, forms: tuple[str, str, str]) -> str:
    return f"{n} {plural(n, forms)}"


def member_year_summary(year: int, summary: dict) -> str:
    """Дружелюбная строка для кабинета члена совета."""
    total = int(summary.get("total", 0))
    if total == 0:
        return (f"В {year} году у тебя пока нет участий — самое время добавить "
                f"первую конференцию, грант, конкурс, стипендию или статью! 🚀")
    parts = [
        with_count(int(summary.get(t, 0)), EVENT_TYPE_FORMS[t])
        for t in SUMMARY_ORDER
        if int(summary.get(t, 0)) > 0
    ]
    return f"В {year} году у тебя {with_count(total, PARTICIPATION_FORMS)}: {', '.join(parts)}. Так держать! 💪"
