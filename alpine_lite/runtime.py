"""Разделяемый статус процесса: poll-loop пишет, TG-бот читает.

Держит аптайм, время последнего цикла опроса и состояние логина alpinbet.
Простые атрибуты под маленьким локом — читаются из потока бота для экрана «Статус».
"""
from __future__ import annotations

import os
import socket
import threading
import time
from datetime import datetime, timezone
from typing import Optional


def sd_notify(state: str) -> None:
    """Отправить сообщение systemd через $NOTIFY_SOCKET (READY=1 / WATCHDOG=1).

    No-op вне systemd (переменная не задана). Нужно для Type=notify + WatchdogSec:
    если WATCHDOG=1 перестаёт приходить (тихий зависон), systemd перезапустит сервис.
    """
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return
    if addr.startswith("@"):  # абстрактный сокет
        addr = "\0" + addr[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.connect(addr)
            sock.sendall(state.encode())
    except OSError:
        pass


class RuntimeStatus:
    def __init__(self):
        self._lock = threading.Lock()
        self.started_monotonic = time.monotonic()
        self.started_at = datetime.now(timezone.utc)
        self.last_cycle_at: Optional[datetime] = None
        self.logged_in = False
        self.last_login_at: Optional[datetime] = None

    def mark_cycle(self):
        with self._lock:
            self.last_cycle_at = datetime.now(timezone.utc)

    def set_logged_in(self, value: bool, when: Optional[datetime] = None):
        with self._lock:
            self.logged_in = value
            if value:
                self.last_login_at = when or datetime.now(timezone.utc)

    def uptime_seconds(self) -> float:
        return time.monotonic() - self.started_monotonic

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "uptime_seconds": time.monotonic() - self.started_monotonic,
                "last_cycle_at": self.last_cycle_at,
                "logged_in": self.logged_in,
                "last_login_at": self.last_login_at,
            }


def human_duration(seconds: float) -> str:
    seconds = int(seconds)
    d, seconds = divmod(seconds, 86400)
    h, seconds = divmod(seconds, 3600)
    m, s = divmod(seconds, 60)
    if d:
        return f"{d}д {h}ч {m}м"
    if h:
        return f"{h}ч {m}м"
    if m:
        return f"{m}м {s}с"
    return f"{s}с"
