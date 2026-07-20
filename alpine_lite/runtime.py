"""Разделяемый статус процесса: poll-loop пишет, TG-бот читает.

Держит аптайм, время последнего цикла опроса и состояние логина alpinbet.
Простые атрибуты под маленьким локом — читаются из потока бота для экрана «Статус».
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from typing import Optional


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
