"""Асинхронная отправка: отдельный поток-воркер + очередь.

Опрос alpinbet НИКОГДА не блокируется медленным/недоступным Telegram/VK — все
внешние отправки (сигналы, правка итога, отчёты) кладутся в очередь, а исполняет
их SenderWorker. TG message_id пишутся в БД постфактум (для правки подписи на итоге).
"""
from __future__ import annotations

import logging
import queue
import threading
from dataclasses import dataclass

from . import senders
from .config import Config
from .store import Destination, Store

log = logging.getLogger("alpine.sending")


@dataclass
class SignalJob:
    source_id: int
    forecast_id: str
    caption_html: str
    caption_plain: str
    image_bytes: bytes | None
    image_url: str
    tg_dests: list[Destination]
    vk_dests: list[Destination]


@dataclass
class OutcomeJob:
    tg_targets: list[tuple[str, int]]
    caption_html: str


@dataclass
class ReportJob:
    text_html: str
    text_plain: str
    dests: list[Destination]


class SenderWorker(threading.Thread):
    def __init__(self, cfg: Config, store: Store, stop_event: threading.Event):
        super().__init__(name="sender-worker", daemon=True)
        self.cfg = cfg
        self.store = store
        self.stop_event = stop_event
        self.q: "queue.Queue" = queue.Queue()

    def submit(self, job) -> None:
        self.q.put(job)

    def pending(self) -> int:
        return self.q.qsize()

    def run(self) -> None:
        log.info("sender-worker запущен")
        while not self.stop_event.is_set():
            try:
                job = self.q.get(timeout=1.0)
            except queue.Empty:
                continue
            try:
                self._handle(job)
            except Exception:  # noqa: BLE001
                log.exception("sender job упал: %s", type(job).__name__)

    def _handle(self, job) -> None:
        if isinstance(job, SignalJob):
            self._send_signal(job)
        elif isinstance(job, OutcomeJob):
            self._send_outcome(job)
        elif isinstance(job, ReportJob):
            self._send_report(job)

    def _send_signal(self, j: SignalJob) -> None:
        # VK — основной канал (шлём первым, гарантируем картинку)
        for d in j.vk_dests:
            try:
                senders.vk_send_photo(self.cfg.vk_token, d.chat_id, j.caption_plain,
                                      j.image_bytes, self.cfg.vk_api_version, self.cfg.http_timeout)
            except Exception as exc:  # noqa: BLE001
                log.error("VK send peer=%s forecast=%s: %s", d.chat_id, j.forecast_id, exc)

        tg_targets: list[tuple[str, int]] = []
        for d in j.tg_dests:
            try:
                mid = senders.tg_send_photo(self.cfg.tg_token, d.chat_id, j.caption_html,
                                            j.image_bytes, j.image_url, self.cfg.http_timeout)
                tg_targets.append((d.chat_id, mid))
            except Exception as exc:  # noqa: BLE001
                log.error("TG send chat=%s forecast=%s: %s", d.chat_id, j.forecast_id, exc)

        if tg_targets:
            self.store.set_signal_tg_targets(j.source_id, j.forecast_id, tg_targets)

    def _send_outcome(self, j: OutcomeJob) -> None:
        for chat_id, mid in j.tg_targets:
            try:
                senders.tg_edit_caption(self.cfg.tg_token, chat_id, mid,
                                        j.caption_html, self.cfg.http_timeout)
            except Exception as exc:  # noqa: BLE001
                log.error("TG edit chat=%s mid=%s: %s", chat_id, mid, exc)

    def _send_report(self, j: ReportJob) -> None:
        for d in j.dests:
            try:
                if d.kind == "tg":
                    senders.tg_send_message(self.cfg.tg_token, d.chat_id,
                                            j.text_html, self.cfg.http_timeout)
                elif d.kind == "vk":
                    senders.vk_send_message(self.cfg.vk_token, d.chat_id, j.text_plain,
                                            self.cfg.vk_api_version, self.cfg.http_timeout)
            except Exception as exc:  # noqa: BLE001
                log.error("report -> %s %s: %s", d.kind, d.chat_id, exc)
