"""Сборка текстов: сигнал, футер итога, отчёты день/неделя/месяц.

Форматы перенесены из старого app.py, чтобы сообщения выглядели идентично.
"""
from __future__ import annotations

import html
import re
from datetime import date
from typing import Optional

from .alpinbet import ParsedForecast, StatRow, parse_day_label

SPORT_EMOJI = {
    "футбол": "⚽️", "баскетбол": "🏀", "хоккей": "🏒", "теннис": "🎾",
    "настольный теннис": "🏓", "волейбол": "🏐", "гандбол": "🤾",
    "бейсбол": "⚾️", "американский футбол": "🏈", "регби": "🏉",
    "киберспорт": "🎮", "бокс": "🥊", "мма": "🥊", "крикет": "🏏",
    "бадминтон": "🏸", "снукер": "🎱", "дартс": "🎯", "водное поло": "🤽",
}


def sport_emoji(sport: Optional[str]) -> str:
    if not sport:
        return "🎯"
    return SPORT_EMOJI.get(sport.strip().lower(), "🎯")


def _esc(text: str) -> str:
    return html.escape(text or "", quote=False)


def status_line(f: ParsedForecast) -> str:
    parts = [p for p in (f.status, f.minute) if p]
    status = " ".join(parts) if parts else "—"
    return f"🕐 {status}    🔢 Счёт: {f.score or '—'}"


# ---------- сигнал ----------
def build_caption_html(f: ParsedForecast) -> str:
    return (
        f"{sport_emoji(f.sport)} {_esc(f.sport or 'Спорт')}\n"
        f"🏆 {_esc(f.league or 'Турнир не указан')}\n"
        f"⚔️ <code>{_esc(f.home_team or '')}</code> - <code>{_esc(f.away_team or '')}</code>\n"
        f"{status_line(f)}\n"
        "------------------------------\n"
        f"📈 Коэффициент: {_esc(f.coefficient or '—')}\n"
        f'🔗 <a href="{html.escape(f.url, quote=True)}">Ссылка на матч</a>'
    )


def build_caption_plain(f: ParsedForecast) -> str:
    """Текст для VK (без HTML)."""
    return (
        f"{sport_emoji(f.sport)} {f.sport or 'Спорт'}\n"
        f"🏆 {f.league or 'Турнир не указан'}\n"
        f"⚔️ {f.home_team} - {f.away_team}\n"
        f"{status_line(f)}\n"
        "------------------------------\n"
        f"📈 Коэффициент: {f.coefficient or '—'}\n"
        f"🔗 {f.url}"
    )


# ---------- итог матча ----------
def outcome_icon(outcome: Optional[str]) -> str:
    return {"win": "✅", "lose": "✖️", "return": "♻️"}.get(outcome or "", "♻️")


def outcome_word(outcome: Optional[str]) -> str:
    return {"win": "Зашло", "lose": "Не зашло", "return": "Возврат"}.get(
        outcome or "", "Возврат")


def format_profit_percent(units: int) -> str:
    sign = "+" if units > 0 else ""
    return f"{sign}{units / 1000:.2f}%"


def settlement_footer(outcome: Optional[str], profit_units: int, score: Optional[str]) -> str:
    return (
        "------------------------------\n"
        f"Итог: {outcome_word(outcome)} {outcome_icon(outcome)}  "
        f"Прибыль: {format_profit_percent(profit_units)}\n"
        f"🔢 Финальный счёт: {score or '—'}"
    )


def append_settlement(caption_html: str, outcome: Optional[str],
                      profit_units: int, score: Optional[str]) -> str:
    return f"{caption_html}\n{settlement_footer(outcome, profit_units, score)}"


# ---------- отчёты ----------
def _icon_by_percent(percent_text: str) -> str:
    val = _percent_number(percent_text)
    if val > 0:
        return "✅"
    if val < 0:
        return "✖️"
    return "♻️"


def _percent_number(text: str) -> float:
    m = re.search(r"-?\d+(?:[.,]\d+)?", (text or "").replace(" ", ""))
    return float(m.group(0).replace(",", ".")) if m else 0.0


def _link(url: str) -> str:
    return f'🔗 <a href="{html.escape(url, quote=True)}">Проверить на alpinbet</a>'


def build_daily_report(title: str, day: date, row: StatRow, url: str) -> str:
    return (
        f"Статистика рассылки {_esc(title)} за {day.strftime('%d.%m.%Y')}\n"
        f"{row.win}✅/{row.lose}✖️/{row.ret}♻️\n"
        f"Прибыль составила {_esc(row.profit_percent)}\n"
        f"{_link(url)}\n"
        "☝️☝️☝️"
    )


def build_weekly_report(title: str, week_start: date, week_end: date,
                        day_rows: list[StatRow], url: str) -> str:
    by_day = {parse_day_label(r.label): r for r in day_rows}
    lines = []
    total = 0.0
    cur = week_start
    while cur <= week_end:
        r = by_day.get(cur)
        pct = r.profit_percent if r else "0.00%"
        total += _percent_number(pct)
        lines.append(f"{cur.strftime('%d.%m')} {_icon_by_percent(pct)}{_esc(pct)}")
        cur = cur.fromordinal(cur.toordinal() + 1)
    sign = "+" if total > 0 else ""
    return (
        f"{_esc(title)}\n"
        "Всем доброго дня!\n"
        f"За прошедшую неделю прибыль составила {sign}{total:.2f}%\n"
        + "\n".join(lines) + "\n\n"
        f"{_link(url)}\n"
        "☝️☝️☝️"
    )


def build_monthly_report(title: str, row: StatRow, url: str) -> str:
    return (
        f"{_esc(title)}\n"
        "Всем доброго дня!\n"
        f"За прошедший месяц прибыль составила {_esc(row.profit_percent)}\n"
        f"{_link(url)}\n"
        "☝️☝️☝️"
    )


def dispatch_title_from_url(url: str) -> str:
    tail = url.rstrip("/").split("/")[-1]
    return tail.replace("-", " ").strip().title() or url
