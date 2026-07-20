"""Один проход по рассылке: разослать новые активные сигналы, дописать итог завершённым.

VK — основной канал, фото с прогнозом, без редактирования.
TG — фото с подписью; при завершении матча подпись редактируется.
"""
from __future__ import annotations

import logging

from . import notifier
from .alpinbet import AlpinbetClient, ParsedForecast
from .config import Config
from .store import Source, Store

log = logging.getLogger("alpine.runner")


def poll_source(client: AlpinbetClient, store: Store, cfg: Config, source: Source) -> dict:
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
            if _send_new(client, store, cfg, source, f, tg_dests, vk_dests):
                sent += 1
            continue

        # уже отправляли — проверяем завершение
        if f.settled and not existing["settled"]:
            _send_outcome(cfg, store, source, existing, f)
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


def _send_new(client, store, cfg, source, f: ParsedForecast, tg_dests, vk_dests) -> bool:
    from . import senders
    image_bytes = _download_image(client, f)
    caption_html = notifier.build_caption_html(f)
    caption_plain = notifier.build_caption_plain(f)

    tg_targets: list[tuple[str, int]] = []
    # VK — основной канал (шлём первым, гарантируем картинку)
    for d in vk_dests:
        try:
            senders.vk_send_photo(cfg.vk_token, d.chat_id, caption_plain,
                                  image_bytes, cfg.vk_api_version, cfg.http_timeout)
        except Exception as exc:  # noqa: BLE001
            log.error("VK send peer=%s forecast=%s: %s", d.chat_id, f.forecast_id, exc)

    for d in tg_dests:
        try:
            mid = senders.tg_send_photo(cfg.tg_token, d.chat_id, caption_html,
                                       image_bytes, f.image_url, cfg.http_timeout)
            tg_targets.append((d.chat_id, mid))
        except Exception as exc:  # noqa: BLE001
            log.error("TG send chat=%s forecast=%s: %s", d.chat_id, f.forecast_id, exc)

    if not tg_targets and not vk_dests:
        return False

    store.create_signal(source.id, f.forecast_id,
                        home_team=f.home_team or "", away_team=f.away_team or "",
                        league=f.league or "", sport=f.sport or "",
                        image_url=f.image_url, caption_html=caption_html,
                        tg_targets=tg_targets)
    return True


def _send_outcome(cfg, store, source, existing_row, f: ParsedForecast) -> None:
    from . import senders
    new_caption = notifier.append_settlement(
        existing_row["caption_html"], f.outcome, f.profit_units, f.score)
    for chat_id, mid in Store.signal_tg_targets(existing_row):
        try:
            senders.tg_edit_caption(cfg.tg_token, chat_id, mid, new_caption, cfg.http_timeout)
        except Exception as exc:  # noqa: BLE001
            log.error("TG edit chat=%s mid=%s: %s", chat_id, mid, exc)
    store.mark_settled(source.id, f.forecast_id, f.outcome or "", f.profit_units)
