"""Сетевые фиксы окружения.

force_ipv4(): заставляет urllib3/requests резолвить только по IPv4. На части
российских хостингов IPv6-маршрут до api.telegram.org мёртв (таймауты getUpdates/
sendMessage), тогда как IPv4 работает. Патч глобальный для процесса; VK/alpinbet
работают по IPv4 и так, поэтому им не вредит. Нужный IPv4 всё равно должен быть
достижим (см. пин api.telegram.org в /etc/hosts на сервере, если DNS отдаёт битый IP).
"""
from __future__ import annotations

import logging
import socket

log = logging.getLogger("alpine.netfix")


def force_ipv4() -> None:
    try:
        import urllib3.util.connection as urllib3_cn
        urllib3_cn.allowed_gai_family = lambda: socket.AF_INET
        log.info("Сеть: включён приоритет IPv4 (allowed_gai_family=AF_INET)")
    except Exception as exc:  # noqa: BLE001
        log.warning("Не удалось включить force IPv4: %s", exc)
