"""Отчёты по прибыли: суточный / недельный / месячный.

Статистика берётся из таблиц «Прибыль» страницы рассылки (день/месяц/год),
которые уже отрендерены — без браузера и без отдельного PJAX.

Автоотправка по часам МСК с дедупом по периоду (stats_sent). Также есть ручной
запуск из бота (force=True, без дедупа).
"""
from __future__ import annotations

import html
import logging
import re
from datetime import date, datetime, timedelta, timezone

from . import notifier
from .alpinbet import AlpinbetClient, StatRow, parse_day_label, parse_month_label
from .config import Config
from .sending import ReportJob, SenderWorker
from .store import Source, Store

log = logging.getLogger("alpine.reports")

MSK = timezone(timedelta(hours=3))


# ---------- периоды ----------
def previous_day(now_msk: datetime) -> date:
    return now_msk.date() - timedelta(days=1)


def previous_week(now_msk: datetime) -> tuple[date, date]:
    monday = now_msk.date() - timedelta(days=now_msk.weekday())
    return monday - timedelta(days=7), monday - timedelta(days=1)


def previous_month(now_msk: datetime) -> tuple[int, int]:
    first = now_msk.date().replace(day=1)
    last_prev = first - timedelta(days=1)
    return last_prev.year, last_prev.month


def _period_key(kind: str, now_msk: datetime) -> str:
    if kind == "day":
        return previous_day(now_msk).isoformat()
    if kind == "week":
        s, e = previous_week(now_msk)
        return f"{s.isoformat()}..{e.isoformat()}"
    if kind == "month":
        y, m = previous_month(now_msk)
        return f"{y}-{m:02d}"
    raise ValueError(kind)


# ---------- HTML -> текст для VK ----------
def _to_plain(text_html: str) -> str:
    text = re.sub(r'<a href="([^"]+)">([^<]+)</a>', r"\2 \1", text_html)
    text = re.sub(r"<[^>]+>", "", text)
    return html.unescape(text)


# ---------- сборка снапшотов ----------
def _build_text(stats: dict[str, list[StatRow]], kind: str, source: Source,
                now_msk: datetime) -> str | None:
    title = notifier.dispatch_title_from_url(source.dispatch_url)
    url = source.dispatch_url
    if kind == "day":
        target = previous_day(now_msk)
        row = next((r for r in stats["day"] if parse_day_label(r.label) == target), None)
        if row is None:
            log.debug("Нет дневной строки за %s (source %s)", target, source.id)
            return None
        return notifier.build_daily_report(title, target, row, url)
    if kind == "week":
        s, e = previous_week(now_msk)
        rows = [r for r in stats["day"]
                if (d := parse_day_label(r.label)) and s <= d <= e]
        if not rows:
            log.debug("Нет недельных данных %s..%s (source %s)", s, e, source.id)
            return None
        return notifier.build_weekly_report(title, s, e, rows, url)
    if kind == "month":
        y, m = previous_month(now_msk)
        row = next((r for r in stats["month"] if parse_month_label(r.label) == (y, m)), None)
        if row is None:
            log.debug("Нет месячной строки за %s-%s (source %s)", y, m, source.id)
            return None
        return notifier.build_monthly_report(title, row, url)
    raise ValueError(kind)


def _enqueue_report(worker: SenderWorker, store: Store, source: Source, text_html: str) -> int:
    text_plain = _to_plain(text_html)
    dests = [d for d in store.list_destinations(source.id) if d.send_reports]
    if dests:
        worker.submit(ReportJob(text_html=text_html, text_plain=text_plain, dests=dests))
    return len(dests)


def run_one(client: AlpinbetClient, store: Store, cfg: Config, source: Source,
            kind: str, worker: SenderWorker, *, force: bool = False) -> str:
    """Собрать отчёт и поставить в очередь отправки. Возвращает статус для бота."""
    now_msk = datetime.now(MSK)
    key = _period_key(kind, now_msk)
    if not force and store.stats_already_sent(source.id, kind, key):
        return "уже отправлен"
    stats = client.fetch_stats_tables(source.dispatch_url)
    text = _build_text(stats, kind, source, now_msk)
    if text is None:
        # период уже завершён, данных нет (не было ставок) — помечаем обработанным,
        # чтобы не дёргать тяжёлую страницу каждые 10 c
        if not force:
            store.mark_stats_sent(source.id, kind, key)
        return "нет данных за период"
    n = _enqueue_report(worker, store, source, text)
    if not force:
        store.mark_stats_sent(source.id, kind, key)
    return f"поставлено в очередь ({n} канал.)"


def maybe_send_scheduled(client: AlpinbetClient, store: Store, cfg: Config,
                         worker: SenderWorker) -> None:
    """Вызывать в каждой итерации poll-loop. Шлёт отчёты по достижении часа МСК.

    Часы и вкл/выкл автоотчётов берутся из настроек (settings), фоллбэк — из .env.
    """
    if not store.get_bool_setting("reports_enabled", True):
        return
    now_msk = datetime.now(MSK)
    daily = store.get_int_setting("daily_hour", cfg.daily_hour)
    weekly = store.get_int_setting("weekly_hour", cfg.weekly_hour)
    monthly = store.get_int_setting("monthly_hour", cfg.monthly_hour)
    for source in store.list_sources(enabled_only=True):
        due: list[str] = []
        if now_msk.hour >= daily:
            due.append("day")
        if now_msk.weekday() == 0 and now_msk.hour >= weekly:
            due.append("week")
        if now_msk.day == 1 and now_msk.hour >= monthly:
            due.append("month")
        for kind in due:
            key = _period_key(kind, now_msk)
            if store.stats_already_sent(source.id, kind, key):
                continue
            try:
                status = run_one(client, store, cfg, source, kind, worker)
                lvl = log.info if status.startswith("поставлено") else log.debug
                lvl("Отчёт %s/%s «%s»: %s", kind, key, source.name, status)
            except Exception as exc:  # noqa: BLE001
                log.error("Отчёт %s source=%s упал: %s", kind, source.id, exc)
