"""Состояние в sqlite3 (stdlib, без ORM). Один файл, потокобезопасный доступ.

Таблицы:
  sources      — рассылки alpinbet (url + закешированные user_id/dispatch_id + статус)
  destinations — куда слать (tg|vk), флаги signals/reports
  signals      — отправленные прогнозы (дедуп + tg-таргеты для редактирования)
  stats_sent   — отметки об отправленных отчётах (дедуп по периоду)
"""
from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL DEFAULT '',
    dispatch_url TEXT NOT NULL UNIQUE,
    user_id TEXT NOT NULL DEFAULT '',
    dispatch_id TEXT NOT NULL DEFAULT '',
    enabled INTEGER NOT NULL DEFAULT 1,
    seeded INTEGER NOT NULL DEFAULT 0,
    last_run_at TEXT NOT NULL DEFAULT '',
    last_error TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS destinations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id INTEGER NOT NULL,
    kind TEXT NOT NULL,                 -- 'tg' | 'vk'
    chat_id TEXT NOT NULL,              -- tg chat_id или vk peer_id
    send_signals INTEGER NOT NULL DEFAULT 1,
    send_reports INTEGER NOT NULL DEFAULT 1,
    UNIQUE(source_id, kind, chat_id),
    FOREIGN KEY(source_id) REFERENCES sources(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id INTEGER NOT NULL,
    forecast_id TEXT NOT NULL,
    home_team TEXT DEFAULT '', away_team TEXT DEFAULT '',
    league TEXT DEFAULT '', sport TEXT DEFAULT '',
    image_url TEXT DEFAULT '',
    caption_html TEXT DEFAULT '',
    tg_targets TEXT DEFAULT '[]',       -- JSON [[chat_id, message_id], ...]
    settled INTEGER NOT NULL DEFAULT 0,
    outcome TEXT DEFAULT '',
    profit_units INTEGER NOT NULL DEFAULT 0,
    sent_at TEXT DEFAULT '', settled_at TEXT DEFAULT '',
    UNIQUE(source_id, forecast_id),
    FOREIGN KEY(source_id) REFERENCES sources(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS stats_sent (
    source_id INTEGER NOT NULL,
    kind TEXT NOT NULL,                 -- 'day' | 'week' | 'month'
    period_key TEXT NOT NULL,
    sent_at TEXT DEFAULT '',
    UNIQUE(source_id, kind, period_key)
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT ''
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Source:
    id: int
    name: str
    dispatch_url: str
    user_id: str
    dispatch_id: str
    enabled: bool
    seeded: bool
    last_run_at: str
    last_error: str


@dataclass
class Destination:
    id: int
    source_id: int
    kind: str
    chat_id: str
    send_signals: bool
    send_reports: bool


class Store:
    def __init__(self, path: str):
        self._lock = threading.RLock()
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA foreign_keys = ON")
        self._db.executescript(SCHEMA)
        self._db.commit()

    def close(self):
        with self._lock:
            self._db.close()

    # ---------- sources ----------
    def _row_to_source(self, r) -> Source:
        return Source(r["id"], r["name"], r["dispatch_url"], r["user_id"],
                      r["dispatch_id"], bool(r["enabled"]), bool(r["seeded"]),
                      r["last_run_at"], r["last_error"])

    def list_sources(self, enabled_only: bool = False) -> list[Source]:
        with self._lock:
            q = "SELECT * FROM sources"
            if enabled_only:
                q += " WHERE enabled = 1"
            return [self._row_to_source(r) for r in self._db.execute(q + " ORDER BY id")]

    def get_source(self, source_id: int) -> Optional[Source]:
        with self._lock:
            r = self._db.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()
            return self._row_to_source(r) if r else None

    def add_source(self, name: str, dispatch_url: str) -> int:
        with self._lock:
            cur = self._db.execute(
                "INSERT OR IGNORE INTO sources(name, dispatch_url) VALUES(?, ?)",
                (name, dispatch_url))
            self._db.commit()
            if cur.lastrowid:
                return cur.lastrowid
            r = self._db.execute("SELECT id FROM sources WHERE dispatch_url=?",
                                 (dispatch_url,)).fetchone()
            return r["id"]

    def set_source_ids(self, source_id: int, user_id: str, dispatch_id: str):
        with self._lock:
            self._db.execute("UPDATE sources SET user_id=?, dispatch_id=? WHERE id=?",
                             (user_id, dispatch_id, source_id))
            self._db.commit()

    def set_enabled(self, source_id: int, enabled: bool):
        with self._lock:
            self._db.execute("UPDATE sources SET enabled=? WHERE id=?",
                             (1 if enabled else 0, source_id))
            self._db.commit()

    def mark_seeded(self, source_id: int):
        with self._lock:
            self._db.execute("UPDATE sources SET seeded=1 WHERE id=?", (source_id,))
            self._db.commit()

    def set_run_status(self, source_id: int, error: str = ""):
        with self._lock:
            self._db.execute("UPDATE sources SET last_run_at=?, last_error=? WHERE id=?",
                             (_now(), error, source_id))
            self._db.commit()

    def delete_source(self, source_id: int):
        with self._lock:
            self._db.execute("DELETE FROM sources WHERE id=?", (source_id,))
            self._db.commit()

    # ---------- destinations ----------
    def add_destination(self, source_id: int, kind: str, chat_id: str,
                        send_signals: bool = True, send_reports: bool = True) -> int:
        with self._lock:
            cur = self._db.execute(
                """INSERT INTO destinations(source_id, kind, chat_id, send_signals, send_reports)
                   VALUES(?,?,?,?,?)
                   ON CONFLICT(source_id, kind, chat_id)
                   DO UPDATE SET send_signals=excluded.send_signals,
                                 send_reports=excluded.send_reports""",
                (source_id, kind, chat_id, int(send_signals), int(send_reports)))
            self._db.commit()
            return cur.lastrowid

    def list_destinations(self, source_id: int) -> list[Destination]:
        with self._lock:
            return [Destination(r["id"], r["source_id"], r["kind"], r["chat_id"],
                                bool(r["send_signals"]), bool(r["send_reports"]))
                    for r in self._db.execute(
                        "SELECT * FROM destinations WHERE source_id=? ORDER BY id", (source_id,))]

    def get_destination(self, dest_id: int) -> Optional[Destination]:
        with self._lock:
            r = self._db.execute("SELECT * FROM destinations WHERE id=?", (dest_id,)).fetchone()
            if not r:
                return None
            return Destination(r["id"], r["source_id"], r["kind"], r["chat_id"],
                               bool(r["send_signals"]), bool(r["send_reports"]))

    def set_destination_flags(self, dest_id: int, send_signals: bool, send_reports: bool):
        with self._lock:
            self._db.execute(
                "UPDATE destinations SET send_signals=?, send_reports=? WHERE id=?",
                (int(send_signals), int(send_reports), dest_id))
            self._db.commit()

    def delete_destination(self, dest_id: int):
        with self._lock:
            self._db.execute("DELETE FROM destinations WHERE id=?", (dest_id,))
            self._db.commit()

    # ---------- signals ----------
    def get_signal(self, source_id: int, forecast_id: str) -> Optional[sqlite3.Row]:
        with self._lock:
            return self._db.execute(
                "SELECT * FROM signals WHERE source_id=? AND forecast_id=?",
                (source_id, forecast_id)).fetchone()

    def create_signal(self, source_id: int, forecast_id: str, *, home_team="",
                      away_team="", league="", sport="", image_url="",
                      caption_html="", tg_targets=None, settled=False,
                      outcome="", profit_units=0) -> None:
        with self._lock:
            self._db.execute(
                """INSERT OR IGNORE INTO signals
                   (source_id, forecast_id, home_team, away_team, league, sport,
                    image_url, caption_html, tg_targets, settled, outcome,
                    profit_units, sent_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (source_id, forecast_id, home_team, away_team, league, sport,
                 image_url, caption_html, json.dumps(tg_targets or []),
                 int(settled), outcome, profit_units, _now()))
            self._db.commit()

    def mark_settled(self, source_id: int, forecast_id: str,
                     outcome: str, profit_units: int) -> None:
        with self._lock:
            self._db.execute(
                """UPDATE signals SET settled=1, outcome=?, profit_units=?, settled_at=?
                   WHERE source_id=? AND forecast_id=?""",
                (outcome, profit_units, _now(), source_id, forecast_id))
            self._db.commit()

    def set_signal_tg_targets(self, source_id: int, forecast_id: str,
                              targets: list[tuple[str, int]]) -> None:
        """Записать tg message_id постфактум (асинхронная отправка воркером)."""
        with self._lock:
            self._db.execute(
                "UPDATE signals SET tg_targets=? WHERE source_id=? AND forecast_id=?",
                (json.dumps([[c, m] for c, m in targets]), source_id, forecast_id))
            self._db.commit()

    @staticmethod
    def signal_tg_targets(row) -> list[tuple[str, int]]:
        try:
            return [(str(c), int(m)) for c, m in json.loads(row["tg_targets"] or "[]")]
        except (ValueError, TypeError):
            return []

    # ---------- счётчики/активность по рассылке (для статуса) ----------
    def counts_since_source(self, source_id: int, since_iso: str) -> tuple[int, int, int, int]:
        """(отправлено, зашло, не зашло, возврат) по рассылке с since_iso."""
        with self._lock:
            r = self._db.execute(
                "SELECT "
                " SUM(CASE WHEN sent_at>=? THEN 1 ELSE 0 END), "
                " SUM(CASE WHEN settled_at>=? AND outcome='win' THEN 1 ELSE 0 END), "
                " SUM(CASE WHEN settled_at>=? AND outcome='lose' THEN 1 ELSE 0 END), "
                " SUM(CASE WHEN settled_at>=? AND outcome='return' THEN 1 ELSE 0 END) "
                "FROM signals WHERE source_id=?",
                (since_iso, since_iso, since_iso, since_iso, source_id)).fetchone()
            return tuple(int(x or 0) for x in r)  # type: ignore[return-value]

    def recent_signals(self, source_id: int, limit: int = 5) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._db.execute(
                "SELECT forecast_id, home_team, away_team, settled, outcome, "
                "profit_units, sent_at FROM signals WHERE source_id=? "
                "ORDER BY sent_at DESC LIMIT ?", (source_id, limit))]

    # ---------- stats dedup ----------
    def stats_already_sent(self, source_id: int, kind: str, period_key: str) -> bool:
        with self._lock:
            return self._db.execute(
                "SELECT 1 FROM stats_sent WHERE source_id=? AND kind=? AND period_key=?",
                (source_id, kind, period_key)).fetchone() is not None

    def mark_stats_sent(self, source_id: int, kind: str, period_key: str):
        with self._lock:
            self._db.execute(
                "INSERT OR IGNORE INTO stats_sent(source_id, kind, period_key, sent_at) "
                "VALUES(?,?,?,?)", (source_id, kind, period_key, _now()))
            self._db.commit()

    # ---------- settings (key/value) ----------
    def get_setting(self, key: str, default: str = "") -> str:
        with self._lock:
            r = self._db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
            return r["value"] if r else default

    def set_setting(self, key: str, value: str):
        with self._lock:
            self._db.execute(
                "INSERT INTO settings(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
            self._db.commit()

    def get_vk_token(self, fallback: str = "") -> str:
        """Актуальный VK-токен: override из БД (задаётся ботом) или fallback из .env."""
        val = self.get_setting("vk_token", "")
        return val if val else fallback

    def is_paused(self) -> bool:
        return self.get_setting("paused", "0") == "1"

    def set_paused(self, paused: bool):
        self.set_setting("paused", "1" if paused else "0")

    def get_int_setting(self, key: str, default: int) -> int:
        try:
            return int(self.get_setting(key, ""))
        except (TypeError, ValueError):
            return default

    def get_bool_setting(self, key: str, default: bool) -> bool:
        raw = self.get_setting(key, "")
        if raw == "":
            return default
        return raw == "1"

    # ---------- счётчики за период (для статуса) ----------
    def counts_since(self, since_iso: str) -> tuple[int, int]:
        """(отправлено сигналов, завершено) с момента since_iso (ISO UTC)."""
        with self._lock:
            sent = self._db.execute(
                "SELECT COUNT(*) FROM signals WHERE sent_at >= ?", (since_iso,)).fetchone()[0]
            settled = self._db.execute(
                "SELECT COUNT(*) FROM signals WHERE settled_at >= ?", (since_iso,)).fetchone()[0]
            return sent, settled


def seed_from_json(store: Store, json_path: str) -> int:
    """Однократно засеять источники из parser_sources.json v2.0 (если таблица пуста)."""
    if store.list_sources():
        return 0
    try:
        with open(json_path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return 0
    from .notifier import dispatch_title_from_url
    added = 0
    for item in data:
        url = item.get("url")
        if not url:
            continue
        sid = store.add_source(dispatch_title_from_url(url), url)
        if item.get("chat_id"):
            store.add_destination(sid, "tg", str(item["chat_id"]))
        vk_peer = item.get("vk_chat_id") or (item.get("vk_chat_ids") or [None])[0]
        if vk_peer:
            store.add_destination(sid, "vk", str(vk_peer))
        if not item.get("enabled", True):
            store.set_enabled(sid, False)
        added += 1
    return added
