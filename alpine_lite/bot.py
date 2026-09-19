"""TG-бот управления — полностью на инлайн-кнопках (без команд).

Навигация:
  Главное меню → Рассылки → [рассылка] → Каналы/Отчёт/Вкл-Выкл/Удалить
Текстовый ввод нужен только для URL рассылки и chat_id канала — бот просит
прислать значение ответным сообщением (pending-состояние на чат).

Доступ — только админам из ADMIN_CHAT_IDS. Мутации стора — сразу; отчёты ставятся
в очередь и исполняются poll-циклом (там живёт сессия alpinbet).
"""
from __future__ import annotations

import json
import logging
import queue
import threading
import time
from datetime import datetime, timedelta, timezone

import requests

from . import senders
from .config import Config
from .notifier import dispatch_title_from_url
from .runtime import RuntimeStatus, human_duration
from .store import Store

MSK = timezone(timedelta(hours=3))

log = logging.getLogger("alpine.bot")
TG_API = "https://api.telegram.org/bot{token}/{method}"


def _btn(text: str, data: str) -> dict:
    return {"text": text, "callback_data": data}


def _kb(rows: list[list[dict]]) -> str:
    return json.dumps({"inline_keyboard": rows})


class ManagementBot(threading.Thread):
    def __init__(self, cfg: Config, store: Store,
                 report_queue: "queue.Queue", control_queue: "queue.Queue",
                 status: RuntimeStatus, stop_event: threading.Event):
        super().__init__(name="bot-loop", daemon=True)
        self.cfg = cfg
        self.store = store
        self.report_queue = report_queue
        self.control_queue = control_queue
        self.status = status
        self.stop_event = stop_event
        self._offset = 0
        self._s = requests.Session()
        self._s.trust_env = False
        # chat_id -> ("add_source",) | ("add_dest", source_id, kind)
        self._pending: dict[int, tuple] = {}

    # ---------- low-level ----------
    def _api(self, method: str, **params):
        r = self._s.post(TG_API.format(token=self.cfg.tg_token, method=method),
                         data=params, timeout=40)
        return r.json()

    def reply(self, chat_id, text: str, keyboard: str | None = None):
        try:
            kw = {"chat_id": chat_id, "text": text, "disable_web_page_preview": "true"}
            if keyboard:
                kw["reply_markup"] = keyboard
            self._api("sendMessage", **kw)
        except Exception as exc:  # noqa: BLE001
            log.error("bot reply chat=%s: %s", chat_id, exc)

    def _edit(self, chat_id, message_id, text: str, keyboard: str | None = None):
        kw = {"chat_id": chat_id, "message_id": message_id, "text": text,
              "disable_web_page_preview": "true"}
        if keyboard:
            kw["reply_markup"] = keyboard
        res = self._api("editMessageText", **kw)
        if not res.get("ok") and "message is not modified" not in str(res.get("description", "")):
            # как фоллбэк — отправить новым сообщением
            self.reply(chat_id, text, keyboard)

    def _answer(self, callback_id, text: str = ""):
        try:
            self._api("answerCallbackQuery", callback_query_id=callback_id, text=text)
        except Exception:  # noqa: BLE001
            pass

    def _vk_token(self) -> str:
        """Актуальный VK-токен: override из БД (задан ботом) или fallback из .env."""
        return self.store.get_vk_token(self.cfg.vk_token)

    @staticmethod
    def _mask_token(token: str) -> str:
        if not token:
            return "— не задан"
        if len(token) <= 12:
            return "•" * len(token)
        return f"{token[:6]}…{token[-4:]} ({len(token)} симв.)"

    def _send_test(self, dest) -> tuple[bool, str]:
        """Реальное тестовое сообщение в канал/беседу. TG/VK — без сессии alpinbet."""
        stamp = time.strftime("%d.%m %H:%M:%S")
        text = (f"🧪 Тест доставки alpine-lite ({stamp}).\n"
                f"Если ты это видишь — канал настроен верно, отправщик работает.")
        try:
            if dest.kind == "tg":
                senders.tg_send_message(self.cfg.tg_token, dest.chat_id, text,
                                        self.cfg.http_timeout)
            elif dest.kind == "vk":
                senders.vk_send_message(self._vk_token(), dest.chat_id, text,
                                        self.cfg.vk_api_version, self.cfg.http_timeout)
            else:
                return False, f"неизвестный тип канала {dest.kind}"
            return True, ""
        except Exception as exc:  # noqa: BLE001
            log.warning("тест доставки %s %s: %s", dest.kind, dest.chat_id, exc)
            return False, str(exc)[:200]

    # ---------- main loop ----------
    def run(self):
        log.info("bot-loop запущен (кнопочный UI)")
        while not self.stop_event.is_set():
            try:
                resp = self._api("getUpdates", offset=self._offset, timeout=25,
                                 allowed_updates=json.dumps(["message", "callback_query"]))
                for upd in resp.get("result", []):
                    self._offset = upd["update_id"] + 1
                    if "callback_query" in upd:
                        self._on_callback(upd["callback_query"])
                    else:
                        msg = upd.get("message")
                        if msg:
                            self._on_message(msg)
            except Exception as exc:  # noqa: BLE001
                log.warning("bot getUpdates: %s", exc)
                time.sleep(3)

    # ---------- messages (старт + текстовый ввод) ----------
    def _on_message(self, msg):
        chat_id = msg["chat"]["id"]
        if not self.cfg.is_admin(chat_id):
            return
        text = (msg.get("text") or "").strip()

        pending = self._pending.pop(chat_id, None)
        if pending:
            self._handle_pending(chat_id, pending, text)
            return

        # любое сообщение / /start -> главное меню
        self.reply(chat_id, self._main_title(), self._main_kb())

    def _handle_pending(self, chat_id, pending, text: str):
        if not text or text.startswith("/"):
            self.reply(chat_id, "Отменено.", self._main_kb())
            return
        action = pending[0]
        if action == "add_source":
            sid = self.store.add_source(dispatch_title_from_url(text), text)
            self.reply(chat_id, f"✅ Рассылка добавлена: {dispatch_title_from_url(text)}",
                       self._source_kb(sid))
            self.reply(chat_id, self._source_title(sid), self._source_kb(sid))
        elif action == "add_dest":
            _, source_id, kind = pending
            self.store.add_destination(source_id, kind, text, True, True)
            self.reply(chat_id, f"✅ Канал {kind.upper()} {text} привязан",
                       self._dest_list_kb(source_id))
        elif action == "set_vk_token":
            token = text.strip()
            # без проверок — просто перезаписываем ключ (VK-валидация падала на Flood control)
            self.store.set_setting("vk_token", token)
            self.reply(chat_id, f"✅ VK-ключ перезаписан: {self._mask_token(token)}\n"
                                f"Применяется на лету — рассылки уже идут с новым ключом.",
                       self._settings_kb())

    # ---------- callbacks ----------
    def _on_callback(self, cb):
        chat_id = cb["message"]["chat"]["id"]
        message_id = cb["message"]["message_id"]
        cb_id = cb["id"]
        if not self.cfg.is_admin(chat_id):
            self._answer(cb_id, "Нет доступа")
            return
        data = cb.get("data", "")
        try:
            self._route(chat_id, message_id, cb_id, data)
        except Exception as exc:  # noqa: BLE001
            log.exception("callback route %s", data)
            self._answer(cb_id, f"Ошибка: {exc}")

    def _route(self, chat_id, mid, cb_id, data: str):
        parts = data.split(":")
        head = parts[0]

        if head == "menu":
            self._answer(cb_id)
            self._edit(chat_id, mid, self._main_title(), self._main_kb())

        elif head == "pause":
            self.store.set_paused(True)
            self._answer(cb_id, "Опрос на паузе")
            self._edit(chat_id, mid, self._main_title(), self._main_kb())

        elif head == "resume":
            self.store.set_paused(False)
            self._answer(cb_id, "Опрос возобновлён")
            self._edit(chat_id, mid, self._main_title(), self._main_kb())

        elif head == "vkchats":
            self._answer(cb_id, "Запрашиваю у VK…")
            self._edit(chat_id, mid, self._vk_chats_text(),
                       _kb([[_btn("🔄 Обновить", "vkchats")], [_btn("⬅️ В меню", "menu:main")]]))

        elif head == "relogin":
            self.control_queue.put(("relogin", chat_id))
            self._answer(cb_id, "Ставлю релогин в очередь…")
            self._edit(chat_id, mid, "🔑 Релогин в alpinbet поставлен в очередь — "
                                     "пришлю результат сюда.", self._main_kb())

        elif head == "slist":
            self._answer(cb_id)
            self._edit(chat_id, mid, "📋 Рассылки:", self._sources_list_kb())

        elif head == "sadd":
            self._pending[chat_id] = ("add_source",)
            self._answer(cb_id)
            self._edit(chat_id, mid, "➕ Пришли ответным сообщением URL рассылки alpinbet\n"
                                     "(напр. https://alpinbet.com/dispatch/id…/slug)",
                       _kb([[_btn("⬅️ Отмена", "slist")]]))

        elif head == "src":
            sid = int(parts[1])
            self._answer(cb_id)
            self._edit(chat_id, mid, self._source_title(sid), self._source_kb(sid))

        elif head == "tgl":
            sid = int(parts[1])
            src = self.store.get_source(sid)
            if src:
                self.store.set_enabled(sid, not src.enabled)
            self._answer(cb_id, "Включено" if (src and not src.enabled) else "Выключено")
            self._edit(chat_id, mid, self._source_title(sid), self._source_kb(sid))

        elif head == "del":
            sid = int(parts[1])
            self._answer(cb_id)
            self._edit(chat_id, mid, f"🗑 Удалить рассылку «{self._src_name(sid)}»?",
                       _kb([[_btn("⚠️ Да, удалить", f"delyes:{sid}")],
                            [_btn("⬅️ Отмена", f"src:{sid}")]]))

        elif head == "delyes":
            sid = int(parts[1])
            self.store.delete_source(sid)
            self._answer(cb_id, "Удалено")
            self._edit(chat_id, mid, "📋 Рассылки:", self._sources_list_kb())

        elif head == "dst":
            sid = int(parts[1])
            self._answer(cb_id)
            self._edit(chat_id, mid, self._dest_list_title(sid), self._dest_list_kb(sid))

        elif head == "dstadd":
            sid, kind = int(parts[1]), parts[2]
            if kind == "vk":
                self._answer(cb_id, "Запрашиваю у VK…")
                self._edit(chat_id, mid, f"➕ Выбери VK-беседу для «{self._src_name(sid)}»:",
                           self._vk_pick_kb(sid))
            else:
                self._pending[chat_id] = ("add_dest", sid, kind)
                self._answer(cb_id)
                self._edit(chat_id, mid, "➕ Пришли ответным сообщением chat_id канала (вида -100…)",
                           _kb([[_btn("⬅️ Отмена", f"dst:{sid}")]]))

        elif head == "dstaddm":  # ручной ввод, если чата нет в списке
            sid, kind = int(parts[1]), parts[2]
            self._pending[chat_id] = ("add_dest", sid, kind)
            hint = ("chat_id канала (вида -100…)" if kind == "tg"
                    else "peer_id беседы VK (обычно 2000000000+id)")
            self._answer(cb_id)
            self._edit(chat_id, mid, f"➕ Пришли ответным сообщением {hint}",
                       _kb([[_btn("⬅️ Отмена", f"dst:{sid}")]]))

        elif head == "vkpick":
            sid, peer_id = int(parts[1]), parts[2]
            self.store.add_destination(sid, "vk", peer_id, True, True)
            self._answer(cb_id, "VK-беседа привязана ✅")
            self._edit(chat_id, mid, self._dest_list_title(sid), self._dest_list_kb(sid))

        elif head == "dv":  # destination view
            did = int(parts[1])
            self._answer(cb_id)
            self._edit(chat_id, mid, self._dest_view_title(did), self._dest_view_kb(did))

        elif head == "dsig":
            did = int(parts[1])
            d = self.store.get_destination(did)
            if d:
                self.store.set_destination_flags(did, not d.send_signals, d.send_reports)
            self._answer(cb_id)
            self._edit(chat_id, mid, self._dest_view_title(did), self._dest_view_kb(did))

        elif head == "drep":
            did = int(parts[1])
            d = self.store.get_destination(did)
            if d:
                self.store.set_destination_flags(did, d.send_signals, not d.send_reports)
            self._answer(cb_id)
            self._edit(chat_id, mid, self._dest_view_title(did), self._dest_view_kb(did))

        elif head == "dtest":
            did = int(parts[1])
            d = self.store.get_destination(did)
            if not d:
                self._answer(cb_id, "Канал не найден")
                return
            ok, detail = self._send_test(d)
            self._answer(cb_id, "Отправлено ✅" if ok else "Ошибка")
            src_name = self._src_name(d.source_id)
            head_txt = (f"🧪 Тест в {d.kind.upper()} {d.chat_id} — "
                        f"{'доставлено ✅' if ok else 'НЕ доставлено ❌'}")
            body = ("Проверь, что сообщение пришло в канал/беседу." if ok
                    else f"Причина: {detail}\nПроверь chat_id/peer_id, права бота и токен.")
            self._edit(chat_id, mid, f"{head_txt}\nКанал «{src_name}».\n{body}",
                       self._dest_view_kb(did))

        elif head == "ddel":
            did = int(parts[1])
            d = self.store.get_destination(did)
            sid = d.source_id if d else None
            self.store.delete_destination(did)
            self._answer(cb_id, "Канал удалён")
            if sid is not None:
                self._edit(chat_id, mid, self._dest_list_title(sid), self._dest_list_kb(sid))
            else:
                self._edit(chat_id, mid, "📋 Рассылки:", self._sources_list_kb())

        elif head == "rep":
            sid = int(parts[1])
            self._answer(cb_id)
            self._edit(chat_id, mid, f"📊 Отчёт по «{self._src_name(sid)}» — за период:",
                       _kb([[_btn("📅 День", f"repd:{sid}:day"),
                             _btn("🗓 Неделя", f"repd:{sid}:week"),
                             _btn("📆 Месяц", f"repd:{sid}:month")],
                            [_btn("⬅️ Назад", f"src:{sid}")]]))

        elif head == "repd":
            sid, kind = int(parts[1]), parts[2]
            self.report_queue.put((chat_id, sid, kind))
            self._answer(cb_id, "Отчёт ставится в очередь…")
            self._edit(chat_id, mid, f"📊 Отчёт ({kind}) для «{self._src_name(sid)}» "
                                     f"поставлен в очередь — пришлю результат сюда.",
                       self._source_kb(sid))

        elif head == "status":
            self._answer(cb_id)
            self._edit(chat_id, mid, self._status_text(),
                       _kb([[_btn("🔄 Обновить", "status")], [_btn("⬅️ В меню", "menu:main")]]))

        elif head == "settings":
            self._answer(cb_id)
            self._edit(chat_id, mid, self._settings_text(), self._settings_kb())

        elif head == "setint":
            self.store.set_setting("interval", parts[1])
            self._answer(cb_id, f"Интервал: {parts[1]} c")
            self._edit(chat_id, mid, self._settings_text(), self._settings_kb())

        elif head == "sethour":
            for k in ("daily_hour", "weekly_hour", "monthly_hour"):
                self.store.set_setting(k, parts[1])
            self._answer(cb_id, f"Час отчётов: {parts[1]}:00 МСК")
            self._edit(chat_id, mid, self._settings_text(), self._settings_kb())

        elif head == "togrep":
            cur = self.store.get_bool_setting("reports_enabled", True)
            self.store.set_setting("reports_enabled", "0" if cur else "1")
            self._answer(cb_id, "Автоотчёты " + ("выкл" if cur else "вкл"))
            self._edit(chat_id, mid, self._settings_text(), self._settings_kb())

        elif head == "vktok":
            self._answer(cb_id)
            self._edit(chat_id, mid, self._vk_token_text(),
                       _kb([[_btn("✏️ Заменить VK-ключ", "vktokset")],
                            [_btn("⬅️ К настройкам", "settings")]]))

        elif head == "vktokset":
            self._pending[chat_id] = ("set_vk_token",)
            self._answer(cb_id)
            self._edit(chat_id, mid, "🔑 Пришли ответным сообщением новый VK user-токен.\n"
                                     "Я проверю его вызовом VK и, если валиден, применю сразу.\n"
                                     "Любое сообщение с «/» — отмена.",
                       _kb([[_btn("⬅️ Отмена", "vktok")]]))

        elif head == "recent":
            sid = int(parts[1])
            self._answer(cb_id)
            self._edit(chat_id, mid, self._recent_text(sid),
                       _kb([[_btn("🔄 Обновить", f"recent:{sid}")],
                            [_btn("⬅️ Назад", f"src:{sid}")]]))

        else:
            self._answer(cb_id)

    # ---------- экраны ----------
    def _main_title(self) -> str:
        n = len(self.store.list_sources())
        poll = "⏸ на паузе" if self.store.is_paused() else "▶️ идёт"
        return f"🎯 Alpine Danil Lite\nОпрос: {poll}\nРассылок: {n}\nВыбери раздел:"

    def _main_kb(self) -> str:
        paused = self.store.is_paused()
        toggle = _btn("▶️ Возобновить опрос", "resume") if paused \
            else _btn("⏸ Пауза опроса", "pause")
        return _kb([
            [toggle],
            [_btn("📋 Рассылки", "slist")],
            [_btn("➕ Добавить рассылку", "sadd")],
            [_btn("🆔 VK-чаты (peer_id)", "vkchats")],
            [_btn("🔑 Перелогиниться в alpinbet", "relogin")],
            [_btn("⚙️ Настройки", "settings")],
            [_btn("ℹ️ Статус", "status")],
        ])

    def _sources_list_kb(self) -> str:
        rows = []
        for s in self.store.list_sources():
            flag = "🟢" if s.enabled else "⚪️"
            rows.append([_btn(f"{flag} {s.name}", f"src:{s.id}")])
        rows.append([_btn("➕ Добавить рассылку", "sadd")])
        rows.append([_btn("⬅️ В меню", "menu:main")])
        return _kb(rows)

    def _src_name(self, sid: int) -> str:
        s = self.store.get_source(sid)
        return s.name if s else f"#{sid}"

    def _source_title(self, sid: int) -> str:
        s = self.store.get_source(sid)
        if not s:
            return "Рассылка не найдена"
        dests = self.store.list_destinations(sid)
        flag = "🟢 включена" if s.enabled else "⚪️ выключена"
        dlines = "\n".join(
            f"  • {d.kind.upper()} {d.chat_id} "
            f"({'sig' if d.send_signals else '–'}/{'rep' if d.send_reports else '–'})"
            for d in dests) or "  • каналов нет"
        since = datetime.now(MSK).replace(hour=0, minute=0, second=0, microsecond=0)
        sent, win, lose, ret = self.store.counts_since_source(
            sid, since.astimezone(timezone.utc).isoformat())
        return (f"📨 {s.name}\n{s.dispatch_url}\nСтатус: {flag}\n"
                f"Сегодня: отправлено {sent}, ✅{win}/✖️{lose}/♻️{ret}\n"
                f"Каналы:\n{dlines}")

    def _source_kb(self, sid: int) -> str:
        s = self.store.get_source(sid)
        toggle = ("⏸ Выключить", f"tgl:{sid}") if (s and s.enabled) else ("▶️ Включить", f"tgl:{sid}")
        return _kb([
            [_btn(*toggle)],
            [_btn("📡 Каналы доставки", f"dst:{sid}")],
            [_btn("📊 Отправить отчёт", f"rep:{sid}")],
            [_btn("🧾 Последние сигналы", f"recent:{sid}")],
            [_btn("🗑 Удалить рассылку", f"del:{sid}")],
            [_btn("⬅️ К списку", "slist")],
        ])

    def _dest_list_title(self, sid: int) -> str:
        return f"📡 Каналы доставки — «{self._src_name(sid)}»\nНажми канал для настройки:"

    def _dest_list_kb(self, sid: int) -> str:
        rows = []
        for d in self.store.list_destinations(sid):
            tags = f"{'sig' if d.send_signals else '–'}/{'rep' if d.send_reports else '–'}"
            rows.append([_btn(f"{d.kind.upper()} {d.chat_id} ({tags})", f"dv:{d.id}")])
        rows.append([_btn("➕ TG-канал", f"dstadd:{sid}:tg"),
                     _btn("➕ VK-беседа", f"dstadd:{sid}:vk")])
        rows.append([_btn("⬅️ Назад", f"src:{sid}")])
        return _kb(rows)

    def _dest_view_title(self, did: int) -> str:
        d = self.store.get_destination(did)
        if not d:
            return "Канал не найден"
        return (f"⚙️ Канал {d.kind.upper()} {d.chat_id}\n"
                f"Сигналы: {'вкл ✅' if d.send_signals else 'выкл'}\n"
                f"Отчёты: {'вкл ✅' if d.send_reports else 'выкл'}")

    def _dest_view_kb(self, did: int) -> str:
        d = self.store.get_destination(did)
        if not d:
            return _kb([[_btn("⬅️ Назад", "slist")]])
        sig = "✅ Сигналы вкл" if d.send_signals else "☑️ Сигналы выкл"
        rep = "✅ Отчёты вкл" if d.send_reports else "☑️ Отчёты выкл"
        return _kb([
            [_btn(sig, f"dsig:{did}")],
            [_btn(rep, f"drep:{did}")],
            [_btn("🧪 Тест доставки", f"dtest:{did}")],
            [_btn("🗑 Удалить канал", f"ddel:{did}")],
            [_btn("⬅️ Назад", f"dst:{d.source_id}")],
        ])

    def _status_text(self) -> str:
        snap = self.status.snapshot()
        poll = "⏸ на паузе" if self.store.is_paused() else "▶️ идёт"

        uptime = human_duration(snap["uptime_seconds"])
        last_cycle = snap["last_cycle_at"]
        cycle_ago = (human_duration((datetime.now(timezone.utc) - last_cycle).total_seconds()) + " назад"
                     if last_cycle else "—")

        if snap["logged_in"]:
            login_at = snap["last_login_at"]
            when = login_at.astimezone(MSK).strftime("%d.%m %H:%M") if login_at else "?"
            login_line = f"✅ залогинен (вход {when} МСК)"
        else:
            login_line = "❌ не залогинен"

        # счётчики за сегодня (с начала суток МСК)
        now_msk = datetime.now(MSK)
        since = now_msk.replace(hour=0, minute=0, second=0, microsecond=0)
        sent, settled = self.store.counts_since(since.astimezone(timezone.utc).isoformat())

        head = (f"ℹ️ Статус\n"
                f"Опрос: {poll}\n"
                f"Процесс: аптайм {uptime}, цикл {cycle_ago}\n"
                f"alpinbet: {login_line}\n"
                f"Сегодня (МСК): отправлено {sent}, завершено {settled}\n")

        lines = []
        for s in self.store.list_sources():
            flag = "🟢" if s.enabled else "⚪️"
            err = f"\n   ⚠️ {s.last_error}" if s.last_error else ""
            lines.append(f"{flag} {s.name} — last_run: {self._fmt_msk(s.last_run_at)}{err}")
        return head + "\nПо рассылкам:\n" + ("\n".join(lines) or "Рассылок нет")

    def _vk_chats_text(self) -> str:
        try:
            convs = senders.vk_list_conversations(
                self._vk_token(), self.cfg.vk_api_version, self.cfg.http_timeout)
        except Exception as exc:  # noqa: BLE001
            return f"🆔 VK-чаты\nНе удалось получить список: {exc}"
        if not convs:
            return "🆔 VK-чаты\nБесед не найдено."
        # сначала беседы (chat), потом остальное — беседы обычно и есть каналы доставки
        convs.sort(key=lambda c: (c["type"] != "chat", str(c["title"]).lower()))
        lines = []
        for c in convs:
            pid = c["peer_id"]
            extra = f"  (chat_id: {pid - 2000000000})" if c["type"] == "chat" else ""
            lines.append(f"• {c['title']} — peer_id: {pid}{extra}")
        return ("🆔 VK-беседы и чаты (для привязки VK-канала бери peer_id):\n\n"
                + "\n".join(lines))

    def _settings_text(self) -> str:
        interval = self.store.get_int_setting("interval", self.cfg.interval)
        dh = self.store.get_int_setting("daily_hour", self.cfg.daily_hour)
        wh = self.store.get_int_setting("weekly_hour", self.cfg.weekly_hour)
        mh = self.store.get_int_setting("monthly_hour", self.cfg.monthly_hour)
        rep = self.store.get_bool_setting("reports_enabled", True)
        hours = f"{dh}:00" if dh == wh == mh else f"день {dh} / нед {wh} / мес {mh}"
        return ("⚙️ Настройки (применяются на лету, без рестарта)\n\n"
                f"⏱ Интервал опроса: {interval} c\n"
                f"🕘 Час автоотчётов (МСК): {hours}\n"
                f"📊 Автоотчёты: {'вкл ✅' if rep else 'выкл ⛔'}\n"
                f"🔑 VK-ключ: {self._mask_token(self._vk_token())}\n\n"
                "Ряд 1 — интервал, ряд 2 — час отчётов:")

    def _settings_kb(self) -> str:
        rep = self.store.get_bool_setting("reports_enabled", True)
        return _kb([
            [_btn("⏱ 10с", "setint:10"), _btn("15с", "setint:15"),
             _btn("30с", "setint:30"), _btn("60с", "setint:60")],
            [_btn("🕘 7", "sethour:7"), _btn("8", "sethour:8"), _btn("9", "sethour:9"),
             _btn("10", "sethour:10"), _btn("12", "sethour:12")],
            [_btn("⛔ Выключить автоотчёты" if rep else "✅ Включить автоотчёты", "togrep")],
            [_btn("🔑 VK-ключ", "vktok")],
            [_btn("⬅️ В меню", "menu:main")],
        ])

    def _vk_token_text(self) -> str:
        token = self._vk_token()
        src = "override из БД (задан ботом)" if self.store.get_setting("vk_token", "") \
            else "из .env (дефолт)"
        return ("🔑 VK-ключ (user-токен для рассылки картинок/сообщений)\n\n"
                f"Текущий: {self._mask_token(token)}\n"
                f"Источник: {src}\n\n"
                "Замена применяется на лету, без рестарта сервиса. "
                "Новый ключ проверяется вызовом VK перед сохранением.")

    def _recent_text(self, sid: int) -> str:
        rows = self.store.recent_signals(sid, 8)
        if not rows:
            return f"🧾 «{self._src_name(sid)}» — последних сигналов нет"
        lines = []
        for r in rows:
            when = self._fmt_msk(r["sent_at"])
            teams = f"{r['home_team']} - {r['away_team']}".strip(" -") or r["forecast_id"]
            if r["settled"]:
                icon = {"win": "✅", "lose": "✖️", "return": "♻️"}.get(r["outcome"], "♻️")
                res = f"{icon} {r['profit_units'] / 1000:+.2f}%"
            else:
                res = "⏳ live"
            lines.append(f"{when}  {teams}  {res}")
        return f"🧾 Последние сигналы «{self._src_name(sid)}»:\n\n" + "\n".join(lines)

    def _vk_pick_kb(self, sid: int) -> str:
        rows: list[list[dict]] = []
        try:
            convs = senders.vk_list_conversations(
                self._vk_token(), self.cfg.vk_api_version, self.cfg.http_timeout)
            convs.sort(key=lambda c: (c["type"] != "chat", str(c["title"]).lower()))
            for c in convs[:20]:
                title = str(c["title"])[:40]
                rows.append([_btn(f"{title} ({c['peer_id']})", f"vkpick:{sid}:{c['peer_id']}")])
        except Exception as exc:  # noqa: BLE001
            log.warning("vk pick list: %s", exc)
        rows.append([_btn("↩️ Ввести peer_id вручную", f"dstaddm:{sid}:vk")])
        rows.append([_btn("⬅️ Назад", f"dst:{sid}")])
        return _kb(rows)

    @staticmethod
    def _fmt_msk(iso: str) -> str:
        if not iso:
            return "—"
        try:
            return datetime.fromisoformat(iso).astimezone(MSK).strftime("%d.%m %H:%M:%S")
        except ValueError:
            return iso
