"""Один проход по рассылке: поставить в очередь новые сигналы и правку итога.

Сама отправка — асинхронная (SenderWorker), поэтому опрос alpinbet не блокируется
медленным TG/VK. Здесь только: скачать картинку, собрать подпись, зафиксировать
сигнал в БД (чтобы не дублировать) и отдать задание воркеру.
"""
from __future__ import annotations

import logging

from . import notifier
from .alpinbet import AlpinbetClient, ParsedForecast
from .config import Config
from .sending import OutcomeJob, SenderWorker, SignalJob
from .store import Source, Store

log = logging.getLogger("alpine.runner")


def poll_source(client: AlpinbetClient, store: Store, cfg: Config,
                source: Source, worker: SenderWorker) -> dict:
    # кеш user_id/dispatch_id
    if not (source.user_id and source.dispatch_id):
        params = client.extract_search_params(source.dispatch_url)
        store.set_source_ids(source.id, params["user_id"], params["dispatch_id"])
        source.user_id, source.dispatch_id = params["user_id"], params["dispatch_id"]

    forecasts = client.fetch_forecasts(
        source.dispatch_url, source.user_id, source.dispatch_id)

    dests = store.list_destinations(source.id)
    tg_dests = [d for d in dests if d.kind == "tg" and d.send_signals]
    vk_dests = [d for d in dests if d.kind == "vk" and d.send_signals]

    # первый проход без рассылки старого: просто пометить активные как «виденные»
    seeding = (not source.seeded) and (not cfg.send_existing_on_start)

    sent = settled = active = 0
    for f in forecasts:
        if not f.forecast_id:
            continue
        if not f.settled:
            active += 1
        existing = store.get_signal(source.id, f.forecast_id)

        if existing is None:
            if f.settled:
                continue  # уже завершён и мы его не слали — не сигналим
            if seeding:
                store.create_signal(source.id, f.forecast_id,
                                    home_team=f.home_team or "", away_team=f.away_team or "",
                                    league=f.league or "", sport=f.sport or "",
                                    image_url=f.image_url,
                                    caption_html=notifier.build_caption_html(f))
                continue
            if _enqueue_new(client, store, source, f, tg_dests, vk_dests, worker):
                sent += 1
            continue

        # уже отправляли — проверяем завершение
        if f.settled and not existing["settled"]:
            _enqueue_outcome(store, source, existing, f, worker)
            settled += 1

    if not source.seeded:
        store.mark_seeded(source.id)
    store.set_run_status(source.id, error="")
    return {"forecasts": len(forecasts), "active": active, "sent": sent, "settled": settled}


def _download_image(client: AlpinbetClient, f: ParsedForecast) -> bytes | None:
    if not f.image_url:
        return None
    try:
        data, _ = client.get_image_bytes(f.image_url)
        return data
    except Exception as exc:  # noqa: BLE001
        log.warning("Не скачал картинку %s: %s", f.image_url, exc)
        return None


def _enqueue_new(client, store, source, f: ParsedForecast, tg_dests, vk_dests,
                 worker: SenderWorker) -> bool:
    if not tg_dests and not vk_dests:
        return False
    image_bytes = _download_image(client, f)
    caption_html = notifier.build_caption_html(f)
    caption_plain = notifier.build_caption_plain(f)

    # фиксируем сигнал ДО отправки (дедуп + место для tg_targets, которые допишет воркер)
    store.create_signal(source.id, f.forecast_id,
                        home_team=f.home_team or "", away_team=f.away_team or "",
                        league=f.league or "", sport=f.sport or "",
                        image_url=f.image_url, caption_html=caption_html)

    worker.submit(SignalJob(
        source_id=source.id, forecast_id=f.forecast_id,
        caption_html=caption_html, caption_plain=caption_plain,
        image_bytes=image_bytes, image_url=f.image_url,
        tg_dests=tg_dests, vk_dests=vk_dests))
    return True


def _enqueue_outcome(store, source, existing_row, f: ParsedForecast,
                     worker: SenderWorker) -> None:
    new_caption = notifier.append_settlement(
        existing_row["caption_html"], f.outcome, f.profit_units, f.score)
    targets = Store.signal_tg_targets(existing_row)
    # помечаем завершённым сразу, чтобы не поставить задание повторно на след. цикле
    store.mark_settled(source.id, f.forecast_id, f.outcome or "", f.profit_units)
    if targets:
        worker.submit(OutcomeJob(tg_targets=targets, caption_html=new_caption))
