"""Test fixtures: a subset of the example 2025 report (for sqlite / sno_test only — never
load into a real database). Used by smoke_test.py and the preview screenshots."""

from __future__ import annotations

import achievements as ach
import db

SNO_NAME = "Сельское хозяйство"
SIGNATORIES = [
    {"position": "Начальник Управления НИРО", "name": "А.В. Вершинина"},
    {"position": "Декан факультета «Агропромышленный»", "name": "Д.В. Рудой"},
    {"position": "Научный наставник", "name": "М.Ю. Одабашян"},
]
PEOPLE = {
    "starostin": "Старостин Дмитрий Владимирович",
    "martynuk": "Мартынюк Игорь Олегович",
    "marchenko": "Марченко Сергей Александрович",
    "sarkisyan": "Саркисян Диана Славиковна",
    "rusanova": "Русанова Диана Максимовна",
    "cholutaeva": "Чолутаева Энкрина Эренценовна",
    "dudarev": "Дударев Артём Игоревич",
    "avdalyan": "Авдалян Эсмина Еремовна",
    "katanaeva": "Катанаева Мария Дмитриевна",
    "gaidai": "Гайдай Родион Романович",
    "polyakov": "Поляков Андрей Геннадьевич",
    "kuralimova": "Куралимова Ксения Сергеевна",
}
CONCLUSION = ("Деятельность СНО «Сельское хозяйство» в 2025 году признана исключительно результативной. "
              "Основная сила общества – высокая публикационная и конкурсная активность участников.")

INTERAGRO = ("VIII Международная научно-практическая конференция «Состояние и перспективы развития "
             "агропромышленного комплекса» (Конференция «ИнтерАгро 2025»)")
FORUM = ("Международный научно-практический форум «Инновационные достижения мировой науки в "
         "сельскохозяйственном производстве»")


def load(db_path=None, password: str = "pass1234") -> dict:  # noqa: ANN001
    """Create members + records; returns {'users': {key: id}, 'admin': admin_user}."""
    db.save_report_settings(SNO_NAME, db.DEFAULT_APPENDIX_LABEL, SIGNATORIES, db_path=db_path)
    uid = {}
    for login, name in PEOPLE.items():
        u = db.get_user_by_login(login, db_path=db_path)
        uid[login] = u["id"] if u else db.create_user(login, password, name, "member", db_path=db_path)
    admin = next(u for u in db.list_users(db_path=db_path) if u["role"] == "admin")
    K = {k["code"]: k for k in ach.list_kinds(db_path, admin=True, include_hidden=True)}
    R = lambda code: ach.row_by_code(code, db_path)["id"]  # noqa: E731
    SP = lambda code: next(s["id"] for k in K.values() for s in k["subpoints"] if s["code"] == code)  # noqa: E731

    def add(kind: str, owner: str | None, **kw) -> int:
        data = {"kind_id": K[kind]["id"], "owner_id": uid[owner] if owner else None, **kw}
        return ach.save_achievement(data, admin, db_path=db_path)["id"]

    # 1. Доклады (два у одного человека на одном мероприятии, один заочный)
    add("doklad", "starostin", row_id=R("I01.intl"), title=INTERAGRO, date_from="2025-02-27",
        topic="Применение пробиотических добавок в комбикормах для сельскохозяйственных животных",
        ochno=True, number="Р-Н-1700-25")
    add("doklad", "starostin", row_id=R("I01.intl"), title=FORUM, date_from="2025-06-11",
        topic="Инновационные достижения мировой науки в сельскохозяйственном производстве",
        ochno=True, number="Р-Н-5637-25")
    add("doklad", "starostin", row_id=R("I01.intl"), title=FORUM, date_from="2025-06-11",
        topic="Как правильно вводить пробиотики в рацион рыб? Методы внесения", ochno=True, number="P-H-5638-25")
    add("doklad", "rusanova", row_id=R("I01.intl"), title=INTERAGRO, date_from="2025-02-27",
        topic="Обзор и анализ энogastрономии в южных регионах России".replace("ogastр", "огастр"),
        ochno=True, number="Р-Н-1754-25")
    add("doklad", "polyakov", row_id=R("I01.intl"), title=FORUM, date_from="2025-06-11",
        topic="Влияние сидератов на повышение биологической активности постагрогенных почв",
        ochno=True, number="Р-Н-5689-25")
    add("doklad", "marchenko", row_id=R("I01.ru"), title="Всероссийская конференция «Молодёжь и наука»",
        date_from="2025-04-18", topic="Гидрохимия прудовых хозяйств", ochno=False, number="Р-Н-3001-25")
    add("doklad", "martynuk", row_id=R("I01.reg"), title="Региональный форум «АгроДон»",
        date_from="2025-10-02", topic="Пробиотики в кормах для рыб", ochno=True, number="Р-Н-8001-25")

    # 2. Публикации (доли участия обязательны; соавторы — строками)
    add("publication", "martynuk", row_id=R("I02.rinc"), title="Методы внесения пробиотиков в комбикорма: обзор",
        owner_share=50, coauthors=[{"user_id": uid["starostin"], "name": PEOPLE["starostin"], "share": 50}],
        authors_text="Мартынюк И.О., Старостин Д.В.",
        journal="Сборник научных трудов ХVIII Международной научно-практической конференции «Интерагромаш»",
        year=2025, link="https://www.elibrary.ru/item.asp?id=82694673",
        doi="10.23947/interagro.2025.117-119", number="Р-Н-5870-25")
    add("publication", "sarkisyan", row_id=R("I02.vak"),
        title="Обзор основных паразитарных и вирусных заболеваний в аквакультуре",
        owner_share=49, coauthors=[{"user_id": uid["cholutaeva"], "name": PEOPLE["cholutaeva"], "share": 48},
                                   {"user_id": None, "name": "Шевченко Виктория Николаевна", "share": 1}],
        journal="Вестник Донецкого национального университета. Серия А: Естественные науки", year=2025,
        link="https://elibrary.ru/item.asp?id=80000001", number="Р-Н-6100-25")
    add("publication", "starostin", row_id=R("I02.scopus"),
        title="Исследование наличия металломагнитных примесей в стартовых комбикормах для аквакультуры",
        owner_share=10, coauthors=[{"user_id": uid["marchenko"], "name": PEOPLE["marchenko"], "share": 10},
                                   {"user_id": uid["martynuk"], "name": PEOPLE["martynuk"], "share": 10}],
        bib=("Starostin, D., Marchenko, S., Martynuk, I., Olshevskaya, A., Odabashyan, M. (2025). Исследование "
             "наличия металломагнитных примесей в стартовых комбикормах для аквакультуры. Siberian Journal of "
             "Life Sciences and Agriculture, 17(6-2)"), journal="Siberian Journal of Life Sciences and Agriculture",
        year=2025, number="Р-Н-9100-25")
    add("publication", "cholutaeva", row_id=R("I02.white"),
        title="Влияние низина на рост и выживаемость годовиков стерляди",
        owner_share=40, coauthors=[{"user_id": uid["sarkisyan"], "name": PEOPLE["sarkisyan"], "share": 30}],
        journal="Рыбоводство и рыбное хозяйство", year=2025, doi="10.33920/sel-09-2510-05")

    # 3. Конкурсы: 3 человека с 3 номерами на одном кейс-чемпионате = 3
    for who, num in (("sarkisyan", "Р-Н-5828-25"), ("cholutaeva", "Р-Н-6768-25"), ("katanaeva", "Р-Н-2973-25")):
        add("contest", who, row_id=R("I03.ru"), title="Всероссийский кейс-чемпионат по биотехнологии и химии ГЕНЕРИУМ",
            date_from="2025-04-03", date_to="2025-04-05", result="участие", number=num)
    add("contest", "dudarev", row_id=R("I03.univ"), title="Индустриальный кейс-марафон «Решаем, внедряем»",
        date_from="2025-05-20", result="диплом II степени", number="Р-П-1444-25")
    add("contest", "kuralimova", row_id=R("I03.univ"), title="Индустриальный кейс-марафон «Решаем, внедряем»",
        date_from="2025-05-20", result="участие", number="Р-П-1445-25")

    # 4. Образовательные мероприятия
    add("edu", "marchenko", row_id=R("I04.intl"), title="V Конгресс молодых ученых", date_from="2025-11-27",
        number="Р-Н-10917-25")
    add("edu", "sarkisyan", row_id=R("I04.ru"), title="Школа научного наставника", date_from="2025-03-07",
        number="Р-Н-1813-25")
    add("edu", "cholutaeva", row_id=R("I04.ru"), title="Научное тестирование «Десятилетие науки и технологий»",
        date_from="2025-07-10", number="Р-Н-5927-25")

    # 8. Стипендии (учебный год)
    add("stipend", "sarkisyan", row_id=R("I08.president"),
        title="Стипендия Президента РФ по приоритетным направлениям", ayear="2024-2025")
    for who in ("marchenko", "katanaeva", "sarkisyan"):
        add("stipend", who, row_id=R("I08.government"), title="Стипендия Правительства РФ", ayear="2025-2026")
    add("stipend", "sarkisyan", row_id=R("I08.regional"), title="Стипендия Губернатора РО", ayear="2025-2026")
    add("stipend", "martynuk", row_id=R("I08.other"),
        title="«Умная стипендия» банка «Центр-инвест» и Фонда целевого капитала «Образование и наука ЮФО»",
        ayear="2024-2025")

    # 7 / 9. Грант: заявка и работы
    add("grant", "gaidai", subpoint_id=SP("grant.app"), row_id=R("I07.ru"), title="Российский научный фонд",
        project="Биосовместимые покрытия на основе тантала", date_from="2025-03-15", status="подана")
    add("grant", "marchenko", subpoint_id=SP("grant.pp"), title="075-03-2025-302/5", date_from="2025-04-22",
        topic="Разработка новой технологии дифференцированной уборки зерновых колосовых культур",
        number="Р-Х-349-25")
    add("grant", "martynuk", subpoint_id=SP("grant.pp"), title="075-03-2024-023/8", date_from="2025-06-06",
        topic="Разработка персонифицированных кормов нового поколения с растительными и пробиотическими "
              "добавками для повышения выживаемости и улучшения здоровья рыб",
        members=[uid["starostin"]], number="Р-Х-353-25")
    add("grant", "gaidai", subpoint_id=SP("grant.rnf"), title="РНФ 25-19-00523", date_from="2025-05-27",
        topic="Комплексные исследования механики биосовместимых покрытий на основе тантала", number="Р-Х-217-25")
    add("grant", "marchenko", subpoint_id=SP("grant.hoz"), title="06-25-УНИ (СНО)", date_from="2025-02-05",
        topic="Исследование гидрохимических параметров среды обитания гидробионтов", number="Р-Х-182-25")

    # 10. Научный обмен
    add("exchange", "rusanova", row_id=R("I10.intl"), title="SPARK Program team", date_from="2025-07-01",
        date_to="2025-08-31")
    add("exchange", "rusanova", row_id=R("I10.intl"), title="Viticulture and Oenology Engineering",
        date_from="2025-09-01", date_to="2026-01-31")

    # 11 / 12. Организация мероприятий (от СНО)
    add("org_sci", None, row_id=R("I11.intl"), title="III Школа молодых учёных", date_from="2025-04-14",
        order_no="462-А", order_date="2025-04-14",
        order_subject="Об организации III Школы молодых ученых, приуроченной к 95-летию ДГТУ")
    add("org_sci", None, row_id=R("I11.intl"), title=FORUM, date_from="2025-06-11", order_no="763-А",
        order_date="2025-06-10", order_subject="Об организации и проведении форума")
    add("org_pop", None, row_id=R("I12.reg"), title="Выставка-демонстрация «День Донского поля»",
        date_from="2025-07-10", order_no="763-А", order_date="2025-07-10",
        order_subject="Об организации и проведении выставки-демонстрации «День Донского поля»")

    # 15. Волонтёрство
    add("volunteer", "dudarev", row_id=R("I15.ru"), title="Мониторинг плодово-ягодных и сельскохозяйственных культур",
        date_from="2025-01-15", number="Р-Н-786-26")
    add("volunteer", "avdalyan", row_id=R("I15.ru"), title="Мониторинг плодово-ягодных и сельскохозяйственных культур",
        date_from="2025-01-15", number="Р-Н-765-26")

    # 16. Конкурс оценки СНО
    add("sno_contest", "marchenko", row_id=R("I16.part"),
        title="Региональный конкурс Российской национальной премии «Студент года – 2025» среди образовательных "
              "организаций высшего образования", date_from="2025-10-20")
    # 17 / 18
    add("agreement", None, title="ООО «АгроИнновации»", agreement="№ 12-С от 03.03.2025",
        subject="совместные исследования кормов для аквакультуры", date_from="2025-03-03")
    add("funded", None, title="Исследование корма для рыб семейства лососевые", source="внебюджет",
        contract="20-25-УНИ", date_from="2025-04-03")
    ach.set_conclusion(2025, CONCLUSION, db_path=db_path)
    return {"users": uid, "admin": admin}
