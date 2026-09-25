# СНО ДГТУ — учёт мероприятий

Веб-приложение для студенческого научного общества ДГТУ (Донской государственный технический университет): учёт участий членов совета в грантах, конференциях и конкурсах.

## Стек

- Python 3.10+
- [Streamlit](https://streamlit.io/), графики — Plotly
- База данных: **Postgres** (например, бесплатный [Neon](https://neon.tech)) или локальная **SQLite** (`data/sno.db`)
- SQLAlchemy 2 + psycopg 3, bcrypt (хэши паролей)

## Как выбирается база данных

1. `DATABASE_URL` из Streamlit Secrets (`.streamlit/secrets.toml` или Secrets в Streamlit Cloud);
2. иначе переменная окружения `DATABASE_URL`;
3. иначе — локальная SQLite `data/sno.db` (создаётся автоматически).

Адреса `postgres://…` и `postgresql://…` (как выдаёт Neon) подходят без изменений.

## Быстрый старт (локально, SQLite)

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
streamlit run app.py
```

Откройте адрес, который покажет Streamlit (обычно http://localhost:8501).

**Администратор при первом запуске.** Если в базе нет ни одного админа, он создаётся автоматически:

- логин — `ADMIN_LOGIN` (по умолчанию `admin`);
- пароль — `ADMIN_PASSWORD`. Только для локальной SQLite без `ADMIN_PASSWORD` используется временный пароль `admin123` — смените его сразу после входа.

На Postgres без `ADMIN_PASSWORD` админ **не создаётся**, а на странице входа показывается подсказка, что нужно задать пароль в Secrets.

Чтобы локально подключиться к Postgres, скопируйте `.streamlit/secrets.toml.example` в `.streamlit/secrets.toml` и заполните значения (или задайте переменные окружения `DATABASE_URL`, `ADMIN_LOGIN`, `ADMIN_PASSWORD`).

## Смена пароля

Любой вошедший пользователь может сменить свой пароль: боковая панель → **🔑 Сменить пароль** (текущий пароль, новый, повтор; не менее 8 символов). Админ также может задать новый пароль любому участнику во вкладке «Участники».

## Развёртывание: Streamlit Community Cloud + Neon (бесплатно)

### 1. База данных в Neon

1. Зарегистрируйтесь на [neon.tech](https://neon.tech) и создайте проект (**New Project**), регион — ближайший (например, Frankfurt / `eu-central-1`).
2. На панели проекта нажмите **Connect**, выберите роль и базу (по умолчанию `neondb`) и скопируйте **connection string** вида
   `postgresql://USER:PASSWORD@ep-xxxx.eu-central-1.aws.neon.tech/neondb?sslmode=require`.
3. Таблицы создавать вручную не нужно — приложение создаст их при первом запуске.

### 2. Код на GitHub

1. Создайте репозиторий на GitHub (можно приватный).
2. Загрузите файлы проекта: `app.py`, `db.py`, `auth.py`, `requirements.txt`, `README.md`, `smoke_test.py`, `.gitignore`, `.streamlit/secrets.toml.example`.
   **Не загружайте** `.venv/`, `data/*.db` и `.streamlit/secrets.toml` — они уже в `.gitignore`.

```bash
git init
git add .
git commit -m "СНО ДГТУ: учёт мероприятий"
git branch -M main
git remote add origin https://github.com/<ваш-логин>/<репозиторий>.git
git push -u origin main
```

### 3. Приложение в Streamlit Community Cloud

1. Зайдите на [share.streamlit.io](https://share.streamlit.io) через GitHub (для приватного репозитория разрешите доступ к нему).
2. **Create app** / **New app** → «Deploy a public app from GitHub»: выберите репозиторий, ветку `main`, **Main file path** — `app.py`.
3. Откройте **Advanced settings**, выберите версию Python (3.11 или 3.12) и в поле **Secrets** вставьте в формате TOML:

```toml
DATABASE_URL = "postgresql://USER:PASSWORD@ep-xxxx.eu-central-1.aws.neon.tech/neondb?sslmode=require"
ADMIN_LOGIN = "admin"
ADMIN_PASSWORD = "придумайте-надёжный-пароль"
```

4. Нажмите **Deploy** и дождитесь сборки.
5. Секреты можно изменить позже: меню приложения → **Settings → Secrets** (после сохранения приложение перезапустится).

### 4. Первый вход

1. Войдите с `ADMIN_LOGIN` / `ADMIN_PASSWORD` из Secrets.
2. Сразу смените пароль: боковая панель → **🔑 Сменить пароль**.
3. Добавьте членов совета во вкладке «Участники».

`ADMIN_PASSWORD` используется только для создания самого первого админа; последующие изменения этого секрета не меняют пароль уже существующего админа.

> Бесплатный Neon «засыпает» при простое — первый запрос после паузы может занять несколько секунд. Streamlit Cloud тоже усыпляет приложения без посещений; данные при этом сохраняются в Neon.

## Роли

- **Админ (лидер СНО)** — управление списком участников (добавление, редактирование, сброс пароля, удаление), статистика с графиками и все участия с фильтрами.
- **Член совета** — добавление и удаление только своих участий.

## Дедупликация мероприятий

Идентичность события: нормализованное название (trim + lower + сжатие пробелов) + тип + дата.

Если такое мероприятие уже есть, новое участие привязывается к нему; иначе создаётся новое. Один человек не может дважды записаться на одно и то же мероприятие. При удалении участника удаляются и его участия.

## Структура проекта

```
├── app.py                        # UI: вход, кабинет члена совета, панель админа
├── auth.py                       # хэш/проверка пароля, authenticate(), change_password()
├── db.py                         # выбор БД (Postgres/SQLite), схема и хелперы
├── data/sno.db                   # локальная SQLite (создаётся автоматически)
├── .streamlit/secrets.toml.example
├── requirements.txt
├── smoke_test.py                 # быстрая проверка логики БД
└── README.md
```

## Smoke-тест (без UI)

```bash
python smoke_test.py
```

Проверяет логику на временной SQLite-базе (рабочая `data/sno.db` не трогается). Ожидаемый вывод: `OK`.

Проверка на Postgres — **только на отдельной тестовой базе**, таблицы в ней будут удалены:

```bash
SMOKE_DATABASE_URL="postgresql://user:pass@localhost/sno_test" python smoke_test.py
```

## Лицензия

Внутренний прототип для СНО ДГТУ.
