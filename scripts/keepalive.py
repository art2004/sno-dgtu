"""Будит и «посещает» Streamlit-сайт настоящим браузером (Playwright/Chromium).

Зачем: Streamlit Community Cloud засыпает приложение после нескольких дней без посетителей, а
«посещением» считается открытая в браузере страница с живым WebSocket - обычный curl не считается.
Скрипт открывает сайт, если видит экран «This app has gone to sleep» - нажимает кнопку пробуждения,
ждёт, пока приложение отрисуется, и держит страницу открытой, чтобы соединение успело установиться.

Запуск: python scripts/keepalive.py [URL]   (URL по умолчанию - переменная SITE_URL или адрес ниже).
Код выхода 0 - сайт проснулся/работает; 1 - не удалось загрузить приложение за отведённое время.
Секреты не нужны и не используются; вход в приложение не выполняется.
"""

from __future__ import annotations

import os
import sys
import time

from playwright.sync_api import sync_playwright

DEFAULT_URL = "https://sno-donstu.streamlit.app/?embed=true"
SLEEP_BUTTON = "button:has-text('get this app back up'), button:has-text('Yes, get this app back up')"
# Streamlit Cloud открывает приложение во вложенном iframe (/~/+/), поэтому смотрим во всех фреймах
APP_READY = "[data-testid='stApp'], [data-testid='stAppViewContainer'], [data-testid='stMain']"
WAKE_TIMEOUT_S = 240    # пробуждение «холодного» приложения (установка зависимостей) может занять минуты
HOLD_S = 25             # сколько держать страницу открытой после загрузки


def _frames(page):  # noqa: ANN001, ANN202
    return [page, *page.frames]


def _any(page, selector: str) -> bool:  # noqa: ANN001
    return any(f.locator(selector).count() for f in _frames(page))


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def main(url: str) -> int:
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 800})
        ws_seen: list[str] = []
        page.on("websocket", lambda ws: ws_seen.append(ws.url))
        log(f"открываю {url}")
        page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        deadline = time.time() + WAKE_TIMEOUT_S
        woke = False
        while time.time() < deadline:
            btn = next((f.locator(SLEEP_BUTTON).first for f in _frames(page) if f.locator(SLEEP_BUTTON).count()), None)
            if btn is not None:
                log("сайт спал - нажимаю кнопку пробуждения")
                btn.click()
                woke = True
                page.wait_for_timeout(5_000)
                continue
            if _any(page, APP_READY):
                break
            page.wait_for_timeout(5_000)
            log("жду загрузку приложения...")
        else:
            page.screenshot(path="keepalive-fail.png")
            log(f"приложение не загрузилось за {WAKE_TIMEOUT_S} с (снимок keepalive-fail.png)")
            browser.close()
            return 1
        log("приложение отрисовано" + (" (было разбужено)" if woke else " (уже работало)"))
        page.wait_for_timeout(HOLD_S * 1000)  # держим соединение
        ok_ws = any("/_stcore/stream" in u for u in ws_seen)
        log(f"WebSocket приложения: {'установлен' if ok_ws else 'НЕ замечен'}")
        if _any(page, SLEEP_BUTTON):
            log("после ожидания снова экран сна - пробуждение не удалось")
            browser.close()
            return 1
        browser.close()
        return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else os.environ.get("SITE_URL", DEFAULT_URL)))
