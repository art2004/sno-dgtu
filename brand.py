"""Фирменный стиль СНО «Сельское хозяйство» (ДГТУ): логотип, шапка, немного CSS.

Цвета и шрифты темы — в .streamlit/config.toml; здесь только то, что тема не умеет.
"""

from __future__ import annotations

import base64
import html
from functools import lru_cache
from pathlib import Path

import plotly.express as px
import streamlit as st

ASSETS = Path(__file__).resolve().parent / "assets"
LOGO = ASSETS / "logo.png"            # логотип (тёмно-зелёный, прозрачный фон)
LOGO_WHITE = ASSETS / "logo_white.png"
ICON = ASSETS / "icon.png"            # favicon
WORDMARK = ASSETS / "wordmark.png"    # «СНО — СЕЛЬСКОЕ ХОЗЯЙСТВО» с обложки ВК

VK_NAME = "Сельское хозяйство"        # название сообщества vk.ru/agriculture_dstu
UNIVERSITY = "Донской государственный технический университет"
PAGE_TITLE = f"СНО «{VK_NAME}» — ДГТУ"

GREEN_DARK, GREEN, GOLD = "#0A3119", "#1B5E34", "#907436"
# same as chartCategoricalColors in config.toml; px.pie etc. take colours from px.defaults
CHART_COLORS = ["#1B5E34", "#907436", "#5B9A45", "#C9A94E", "#0A3119", "#9CBF8B", "#6B5526", "#D9CBA0"]
px.defaults.color_discrete_sequence = CHART_COLORS


@lru_cache(maxsize=None)
def _b64(path: Path) -> str:
    return base64.b64encode(path.read_bytes()).decode("ascii")


def sno_name(db) -> str:  # noqa: ANN001
    """Название из «Настройки отчёта», если его задали; иначе название группы ВК."""
    try:
        name = (db.get_app_setting("sno_name") or "").strip()
    except Exception:  # noqa: BLE001
        name = ""
    return name if name and name != db.DEFAULT_SNO_NAME else VK_NAME


_CSS = """
<style>
.sno-hero {
  position: relative; overflow: hidden;
  display: flex; align-items: center; gap: 1.5rem;
  padding: 1.6rem 2rem; margin: 0 0 1.5rem;
  border-radius: 18px; color: #fff;
  background: linear-gradient(120deg, #0A3119 0%, #1B5E34 62%, #2F7A3E 100%);
  box-shadow: 0 10px 28px rgba(10, 49, 25, .18);
}
.sno-hero::after {            /* крупный полупрозрачный логотип-водяной знак справа */
  content: ""; position: absolute; right: -40px; top: -30px;
  width: 260px; height: 260px; opacity: .09;
  background: var(--sno-watermark) no-repeat center / contain;
  pointer-events: none;
}
.sno-hero .sno-logo {
  flex: 0 0 auto; width: 92px; height: 92px; padding: 14px;
  border-radius: 50%; background: #fff;
  box-shadow: 0 0 0 4px rgba(201, 169, 78, .55);
}
.sno-hero .sno-kicker {
  color: #E6D39A; font-size: .78rem; font-weight: 600;
  letter-spacing: .22em; text-transform: uppercase; margin: 0 0 .35rem;
}
.sno-hero h1 {
  color: #fff !important; margin: 0 !important; padding: 0 !important;
  font-family: "Unbounded", "Montserrat", sans-serif !important; font-weight: 600 !important;
  font-size: 1.75rem !important; line-height: 1.25 !important;
}
.sno-hero .sno-rule { width: 64px; height: 2px; background: #C9A94E; margin: .7rem 0 .55rem; }
.sno-hero .sno-sub { color: #F4F1E6; font-size: .95rem; margin: 0; }
.sno-hero.sno-compact { padding: .7rem 1.1rem; gap: .9rem; margin-bottom: 1rem; border-radius: 14px; }
.sno-hero.sno-compact .sno-logo { width: 46px; height: 46px; padding: 7px; box-shadow: 0 0 0 3px rgba(201,169,78,.55); }
.sno-hero.sno-compact .sno-name { font-family: "Unbounded", "Montserrat", sans-serif; font-weight: 500; font-size: .95rem; color: #fff; }
.sno-hero.sno-compact .sno-sub { font-size: .8rem; color: #E6D39A; letter-spacing: .08em; }
.sno-hero.sno-compact::after { width: 140px; height: 140px; top: -30px; right: -20px; }
@media (max-width: 640px) {
  .sno-hero { flex-direction: column; text-align: center; padding: 1.3rem 1rem; gap: .9rem; }
  .sno-hero .sno-rule { margin-left: auto; margin-right: auto; }
  .sno-hero h1 { font-size: 1.15rem !important; }
  .sno-hero.sno-compact { flex-direction: row; text-align: left; }
}
/* карточки: формы, раскрывающиеся блоки, показатели */
[data-testid="stForm"] { border-radius: 16px; border-color: #E2DCC8; box-shadow: 0 2px 10px rgba(28, 43, 33, .05); }
[data-testid="stExpander"] details { border-radius: 14px; border-color: #E2DCC8; }
[data-testid="stMetric"] {
  background: #F4F1E6; border-radius: 14px; padding: .75rem 1rem;
  border-left: 4px solid #907436;
}
[data-testid="stMetricLabel"] p { color: #5F6B63; }
/* вкладки: активная — фирменный зелёный */
button[data-baseweb="tab"][aria-selected="true"] p { color: #1B5E34; font-weight: 600; }
</style>
"""


def inject_css() -> None:
    st.html(_CSS)  # only <style> → no layout space


def _hero_style() -> str:
    return f'style="--sno-watermark: url(data:image/png;base64,{_b64(LOGO_WHITE)})"'


def login_header(name: str) -> None:
    st.html(
        f"""
        <div class="sno-hero" {_hero_style()}>
          <img class="sno-logo" src="data:image/png;base64,{_b64(LOGO)}" alt="Логотип СНО">
          <div>
            <p class="sno-kicker">Студенческое научное общество · ДГТУ</p>
            <h1>СНО «{html.escape(name)}»</h1>
            <div class="sno-rule"></div>
            <p class="sno-sub">{UNIVERSITY} · учёт мероприятий</p>
          </div>
        </div>
        """
    )


def compact_header(name: str) -> None:
    st.html(
        f"""
        <div class="sno-hero sno-compact" {_hero_style()}>
          <img class="sno-logo" src="data:image/png;base64,{_b64(LOGO)}" alt="Логотип СНО">
          <div>
            <div class="sno-name">СНО «{html.escape(name)}»</div>
            <div class="sno-sub">ДГТУ · учёт мероприятий</div>
          </div>
        </div>
        """
    )


def sidebar_logo() -> None:
    st.logo(str(WORDMARK), size="large", icon_image=str(LOGO))
