"""Тесты периодов отчётов и форматирования текстов."""
from datetime import datetime, timezone, timedelta

from alpine_lite import notifier, reports

MSK = timezone(timedelta(hours=3))


def test_previous_periods():
    now = datetime(2026, 7, 20, 9, 0, tzinfo=MSK)  # понедельник
    assert reports.previous_day(now).isoformat() == "2026-07-19"
    ws, we = reports.previous_week(now)
    assert (ws.isoformat(), we.isoformat()) == ("2026-07-13", "2026-07-19")
    assert reports.previous_month(now) == (2026, 6)


def test_period_key():
    now = datetime(2026, 7, 20, 9, 0, tzinfo=MSK)
    assert reports._period_key("day", now) == "2026-07-19"
    assert reports._period_key("week", now) == "2026-07-13..2026-07-19"
    assert reports._period_key("month", now) == "2026-06"


def test_previous_month_january():
    now = datetime(2026, 1, 5, 9, 0, tzinfo=MSK)
    assert reports.previous_month(now) == (2025, 12)


def test_profit_percent_format():
    assert notifier.format_profit_percent(3500) == "+3.50%"
    assert notifier.format_profit_percent(-1000) == "-1.00%"
    assert notifier.format_profit_percent(0) == "0.00%"


def test_dispatch_title_from_url():
    assert notifier.dispatch_title_from_url(
        "https://alpinbet.com/dispatch/id1631660353/pbd-1-fon") == "Pbd 1 Fon"


def test_sport_emoji_and_outcome():
    assert notifier.sport_emoji("Футбол") == "⚽️"
    assert notifier.sport_emoji("что-то") == "🎯"
    assert notifier.outcome_word("win") == "Зашло"
    assert notifier.outcome_icon("lose") == "✖️"


def test_to_plain_strips_html():
    html = '📈 Коэф: 2.43\n🔗 <a href="http://x">Ссылка</a>'
    plain = reports._to_plain(html)
    assert "<a" not in plain and "Ссылка" in plain and "http://x" in plain
