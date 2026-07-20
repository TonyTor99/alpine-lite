"""Конфигурация из .env. Никаких секретов в коде — всё из окружения."""
from __future__ import annotations

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()


def _int(name: str, default: int, minimum: int | None = None) -> int:
    try:
        val = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        val = default
    if minimum is not None and val < minimum:
        val = minimum
    return val


def _hour(name: str, default: int) -> int:
    val = _int(name, default)
    return val if 0 <= val <= 23 else default


@dataclass
class Config:
    # alpinbet ЛК
    login_username: str = field(default_factory=lambda: os.getenv("TARGET_LOGIN_USERNAME", ""))
    login_password: str = field(default_factory=lambda: os.getenv("TARGET_LOGIN_PASSWORD", ""))

    # Telegram
    tg_token: str = field(default_factory=lambda: os.getenv("TELEGRAM_BOT_TOKEN", ""))
    # VK
    vk_token: str = field(default_factory=lambda: os.getenv("VK_USER_TOKEN", ""))
    vk_api_version: str = field(default_factory=lambda: os.getenv("VK_API_VERSION", "5.199"))

    # доступ к боту управления (id чатов через запятую)
    admin_chat_ids: tuple[str, ...] = field(default_factory=lambda: tuple(
        x.strip() for x in os.getenv("ADMIN_CHAT_IDS", "").split(",") if x.strip()))

    # рантайм
    interval: int = field(default_factory=lambda: _int("PARSER_INTERVAL_SECONDS", 10, minimum=10))
    send_existing_on_start: bool = field(
        default_factory=lambda: os.getenv("PARSER_SEND_EXISTING_ON_START", "0") == "1")
    http_timeout: int = field(default_factory=lambda: _int("HTTP_TIMEOUT_SECONDS", 20, minimum=5))
    # антиспам служебных уведомлений об одной и той же проблеме (минуты)
    alert_repeat_minutes: int = field(
        default_factory=lambda: _int("ALERT_REPEAT_MINUTES", 15, minimum=1))

    # часы автоотправки отчётов (МСК)
    daily_hour: int = field(default_factory=lambda: _hour("DAILY_STATS_SEND_HOUR_MSK", 9))
    weekly_hour: int = field(default_factory=lambda: _hour("WEEKLY_STATS_SEND_HOUR_MSK", 9))
    monthly_hour: int = field(default_factory=lambda: _hour("MONTHLY_STATS_SEND_HOUR_MSK", 9))

    log_level: str = field(default_factory=lambda: os.getenv("APP_LOG_LEVEL", "INFO").upper())
    db_path: str = field(default_factory=lambda: os.getenv("STATE_DB_PATH", "state.db"))

    def validate(self) -> list[str]:
        problems = []
        if not self.login_username or not self.login_password:
            problems.append("TARGET_LOGIN_USERNAME / TARGET_LOGIN_PASSWORD не заданы")
        if not self.tg_token:
            problems.append("TELEGRAM_BOT_TOKEN не задан")
        if not self.vk_token:
            problems.append("VK_USER_TOKEN не задан (VK — основной канал)")
        if not self.admin_chat_ids:
            problems.append("ADMIN_CHAT_IDS не задан — боту управления некого слушать")
        return problems

    def is_admin(self, chat_id) -> bool:
        return str(chat_id) in self.admin_chat_ids
