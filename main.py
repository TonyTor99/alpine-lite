"""Точка входа: один лёгкий процесс.

Поток 1 (главный) — poll-loop: опрос рассылок раз в PARSER_INTERVAL_SECONDS,
рассылка сигналов, отчёты по расписанию, ручные отчёты и релогин из очередей,
служебные уведомления админам при сбоях.
Поток 2 — TG-бот управления (long-poll getUpdates).
"""
from __future__ import annotations

import logging
import os
import queue
import signal
import sys
import threading
import time

import requests

from alpine_lite import reports, runner, senders
from alpine_lite.alpinbet import AlpinbetAuthError, AlpinbetClient
from alpine_lite.bot import ManagementBot
from alpine_lite.config import Config
from alpine_lite.runtime import RuntimeStatus
from alpine_lite.store import Store, seed_from_json

HERE = os.path.dirname(os.path.abspath(__file__))


def setup_logging(level: str):
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )


def classify_error(exc: Exception) -> tuple[str, str]:
    """(kind, человекочитаемая шапка) для служебного уведомления."""
    if isinstance(exc, AlpinbetAuthError):
        return "auth", "🔓 Логин в alpinbet слетел"
    status_code = None
    resp = getattr(exc, "response", None)
    if resp is not None:
        status_code = getattr(resp, "status_code", None)
    if status_code in (403, 429, 503) or isinstance(exc, requests.exceptions.RetryError):
        return "throttle", f"🚫 Похоже на троттлинг/блокировку alpinbet (HTTP {status_code or '—'})"
    if isinstance(exc, (requests.ConnectionError, requests.Timeout)):
        return "network", "🌐 alpinbet недоступен (сеть/таймаут)"
    return "error", "⚠️ Ошибка опроса рассылки"


def drain_report_queue(client, store, cfg, report_queue, reply):
    while True:
        try:
            chat_id, sid, kind = report_queue.get_nowait()
        except queue.Empty:
            return
        source = store.get_source(sid)
        if not source:
            reply(chat_id, f"Рассылка {sid} исчезла")
            continue
        try:
            status = reports.run_one(client, store, cfg, source, kind, force=True)
            reply(chat_id, f"Отчёт {kind} [{sid}]: {status}")
        except Exception as exc:  # noqa: BLE001
            log.exception("ручной отчёт упал")
            reply(chat_id, f"Отчёт {kind} [{sid}] упал: {exc}")


def drain_control_queue(client, cfg, status, control_queue, reply):
    """Команды из бота, требующие сессии alpinbet (живёт здесь). Сейчас — релогин."""
    while True:
        try:
            cmd, chat_id = control_queue.get_nowait()
        except queue.Empty:
            return
        if cmd == "relogin":
            try:
                client.force_login()
                status.set_logged_in(True)
                reply(chat_id, "🔑 Перелогинился в alpinbet ✅")
                log.info("Ручной релогин выполнен")
            except Exception as exc:  # noqa: BLE001
                status.set_logged_in(False)
                log.exception("ручной релогин упал")
                reply(chat_id, f"🔑 Релогин не удался: {exc}")


def main():
    global log
    cfg = Config()
    setup_logging(cfg.log_level)
    log = logging.getLogger("alpine.main")

    problems = cfg.validate()
    for p in problems:
        log.warning("config: %s", p)

    store = Store(os.path.join(HERE, cfg.db_path) if not os.path.isabs(cfg.db_path) else cfg.db_path)
    added = seed_from_json(store, os.path.join(HERE, "sources.json"))
    if added:
        log.info("Засеяно источников из sources.json: %d", added)

    client = AlpinbetClient(cfg.login_username, cfg.login_password,
                            timeout=cfg.http_timeout, throttle=1.0)
    status = RuntimeStatus()

    stop_event = threading.Event()
    report_queue: "queue.Queue" = queue.Queue()
    control_queue: "queue.Queue" = queue.Queue()

    bot = ManagementBot(cfg, store, report_queue, control_queue, status, stop_event)
    bot.start()

    def _stop(*_):
        log.info("Останавливаюсь…")
        stop_event.set()
    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    # ранний логин: сразу знаем, живы ли креды, и наполняем статус
    try:
        client.login()
        status.set_logged_in(True)
        log.info("Стартовый логин в alpinbet выполнен")
    except Exception as exc:  # noqa: BLE001
        status.set_logged_in(False)
        log.error("Стартовый логин не удался: %s", exc)
        senders.notify_admins(cfg.tg_token, cfg.admin_chat_ids,
                              f"🔓 Стартовый вход в alpinbet не удался:\n{str(exc)[:300]}",
                              cfg.http_timeout)

    repeat_sec = cfg.alert_repeat_minutes * 60
    alert_last: dict[str, float] = {}     # signature -> monotonic последнего уведомления
    alert_active: dict[int, str] = {}     # source_id -> шапка активной проблемы (для «снова онлайн»)

    def _maybe_alert(source, exc):
        kind, header = classify_error(exc)
        sig = f"{source.id}:{kind}"
        now = time.monotonic()
        alert_active[source.id] = header
        last = alert_last.get(sig)
        if last is None or (now - last) >= repeat_sec:
            alert_last[sig] = now
            senders.notify_admins(
                cfg.tg_token, cfg.admin_chat_ids,
                f"{header}\nРассылка «{source.name}»:\n{str(exc)[:300]}",
                cfg.http_timeout)

    def _clear_alert(source):
        if source.id in alert_active:
            alert_active.pop(source.id, None)
            for k in list(alert_last):
                if k.startswith(f"{source.id}:"):
                    alert_last.pop(k, None)
            senders.notify_admins(cfg.tg_token, cfg.admin_chat_ids,
                                  f"✅ «{source.name}» снова онлайн — сигналы идут",
                                  cfg.http_timeout)

    log.info("poll-loop запущен (интервал %d c)", cfg.interval)
    while not stop_event.is_set():
        start = time.monotonic()

        # очереди из бота
        drain_report_queue(client, store, cfg, report_queue, bot.reply)
        drain_control_queue(client, cfg, status, control_queue, bot.reply)

        if store.is_paused():
            status.mark_cycle()
            stop_event.wait(max(1.0, cfg.interval - (time.monotonic() - start)))
            continue

        # опрос рассылок
        for source in store.list_sources(enabled_only=True):
            if stop_event.is_set():
                break
            try:
                res = runner.poll_source(client, store, cfg, source)
                extra = ""
                if res.get("sent") or res.get("settled"):
                    extra = f" | новых: {res['sent']}, завершено: {res['settled']}"
                log.info("«%s»: активных матчей %d%s", source.name, res["active"], extra)
                _clear_alert(source)
            except Exception as exc:  # noqa: BLE001
                log.exception("source[%s] упал", source.id)
                store.set_run_status(source.id, error=str(exc)[:500])
                _maybe_alert(source, exc)

        # отчёты по расписанию
        try:
            reports.maybe_send_scheduled(client, store, cfg)
        except Exception:  # noqa: BLE001
            log.exception("планировщик отчётов упал")

        status.set_logged_in(client.logged_in)
        status.mark_cycle()
        elapsed = time.monotonic() - start
        stop_event.wait(max(1.0, cfg.interval - elapsed))

    store.close()
    log.info("Остановлено.")


if __name__ == "__main__":
    main()
