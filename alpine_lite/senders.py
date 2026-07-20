"""Отправка в Telegram и ВКонтакте (на чистом requests).

TG: sendPhoto с подписью + editMessageCaption при завершении матча + sendMessage (отчёты).
VK: фото с прогнозом (getMessagesUploadServer -> upload -> saveMessagesPhoto ->
    messages.send с attachment). Картинка — приоритет; при сбое уходит текст без фото.
"""
from __future__ import annotations

import logging
import random
import time

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

log = logging.getLogger("alpine.senders")

TG_API = "https://api.telegram.org/bot{token}/{method}"
VK_API = "https://api.vk.com/method/{method}"

_session = requests.Session()
_session.trust_env = False
_retry = Retry(total=3, connect=3, read=2, backoff_factor=0.6,
               status_forcelist=(500, 502, 503, 504),
               allowed_methods=frozenset(["GET", "POST"]))
_adapter = HTTPAdapter(max_retries=_retry, pool_connections=4, pool_maxsize=8)
_session.mount("https://", _adapter)
_session.mount("http://", _adapter)


# ===================== Telegram =====================
def _tg_call(token: str, method: str, *, data=None, files=None, timeout=25):
    r = _session.post(TG_API.format(token=token, method=method),
                      data=data, files=files, timeout=timeout)
    payload = r.json()
    if not payload.get("ok"):
        raise RuntimeError(f"TG {method}: {payload.get('description')}")
    return payload["result"]


def tg_send_photo(token: str, chat_id: str, caption_html: str,
                  image_bytes: bytes | None, image_url: str = "",
                  timeout: int = 25) -> int:
    """Фото с подписью. Если есть байты — multipart-upload; иначе по URL;
    если фото вовсе нет — обычное текстовое сообщение."""
    data = {"chat_id": chat_id, "caption": caption_html,
            "parse_mode": "HTML"}
    if image_bytes:
        try:
            res = _tg_call(token, "sendPhoto", data=data,
                           files={"photo": ("match.jpg", image_bytes)}, timeout=timeout)
            return res["message_id"]
        except Exception as exc:  # noqa: BLE001
            log.warning("TG sendPhoto(upload) chat=%s: %s — пробую по URL", chat_id, exc)
    if image_url:
        try:
            res = _tg_call(token, "sendPhoto",
                           data={**data, "photo": image_url}, timeout=timeout)
            return res["message_id"]
        except Exception as exc:  # noqa: BLE001
            log.warning("TG sendPhoto(url) chat=%s: %s — шлю текстом", chat_id, exc)
    return tg_send_message(token, chat_id, caption_html, timeout=timeout)


def tg_edit_caption(token: str, chat_id: str, message_id: int,
                    caption_html: str, timeout: int = 25) -> None:
    try:
        _tg_call(token, "editMessageCaption", data={
            "chat_id": chat_id, "message_id": message_id,
            "caption": caption_html, "parse_mode": "HTML",
        }, timeout=timeout)
    except RuntimeError as exc:
        if "not modified" in str(exc):
            return
        # сообщение могло быть отправлено текстом (фото не было) — правим текст
        if "no caption" in str(exc).lower() or "message to edit" in str(exc).lower():
            _tg_call(token, "editMessageText", data={
                "chat_id": chat_id, "message_id": message_id,
                "text": caption_html, "parse_mode": "HTML",
                "disable_web_page_preview": "true",
            }, timeout=timeout)
            return
        raise


def tg_send_message(token: str, chat_id: str, text_html: str, timeout: int = 25) -> int:
    res = _tg_call(token, "sendMessage", data={
        "chat_id": chat_id, "text": text_html, "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    }, timeout=timeout)
    return res["message_id"]


# ===================== VK =====================
def _vk_call(token: str, method: str, params: dict, api_version: str, timeout=25):
    payload = {**params, "access_token": token, "v": api_version}
    r = _session.post(VK_API.format(method=method), data=payload, timeout=timeout)
    data = r.json()
    if "error" in data:
        raise RuntimeError(f"VK {method}: {data['error'].get('error_msg')}")
    return data["response"]


def _vk_upload_photo(token: str, peer_id: str, image_bytes: bytes,
                     api_version: str, timeout: int) -> str:
    """Загружает фото для сообщения и возвращает attachment 'photo{owner}_{id}'."""
    server = _vk_call(token, "photos.getMessagesUploadServer",
                      {"peer_id": peer_id}, api_version, timeout)
    upload_url = server.get("upload_url")
    if not upload_url:
        raise RuntimeError("VK не вернул upload_url")
    up = _session.post(upload_url, files={"photo": ("match.jpg", image_bytes)},
                       timeout=timeout)
    up_data = up.json()
    if not up_data.get("photo") or up_data.get("server") is None or not up_data.get("hash"):
        raise RuntimeError(f"VK upload вернул неполный ответ: {up_data}")
    saved = _vk_call(token, "photos.saveMessagesPhoto", {
        "photo": up_data["photo"], "server": up_data["server"], "hash": up_data["hash"],
    }, api_version, timeout)
    item = saved[0]
    return f"photo{item['owner_id']}_{item['id']}"


def vk_send_photo(token: str, peer_id: str, text: str,
                  image_bytes: bytes | None, api_version: str = "5.199",
                  timeout: int = 25, upload_retries: int = 2) -> int:
    """Сообщение в VK с картинкой прогноза. Гарантированно доставляем сигнал:
    при неудаче загрузки фото шлём текст без вложения."""
    attachment = ""
    if image_bytes:
        for attempt in range(1, upload_retries + 1):
            try:
                attachment = _vk_upload_photo(token, peer_id, image_bytes,
                                              api_version, timeout)
                break
            except Exception as exc:  # noqa: BLE001
                log.warning("VK upload фото peer=%s попытка %d/%d: %s",
                            peer_id, attempt, upload_retries, exc)
                time.sleep(1.0 * attempt)
        else:
            log.error("VK: не удалось загрузить фото peer=%s — шлю текстом", peer_id)
    params = {
        "peer_id": peer_id, "message": text,
        "random_id": random.randint(1, 2_000_000_000),
        "dont_parse_links": 1,
    }
    if attachment:
        params["attachment"] = attachment
    return _vk_call(token, "messages.send", params, api_version, timeout)


def vk_send_message(token: str, peer_id: str, text: str,
                    api_version: str = "5.199", timeout: int = 25) -> int:
    return _vk_call(token, "messages.send", {
        "peer_id": peer_id, "message": text,
        "random_id": random.randint(1, 2_000_000_000), "dont_parse_links": 1,
    }, api_version, timeout)


# ===================== служебные уведомления админам =====================
def notify_admins(token: str, admin_chat_ids, text: str, timeout: int = 20) -> None:
    """Разослать служебное сообщение всем админам (в личку боту). Best-effort."""
    for chat_id in admin_chat_ids:
        try:
            tg_send_message(token, str(chat_id), text, timeout=timeout)
        except Exception as exc:  # noqa: BLE001
            log.error("notify_admins chat=%s: %s", chat_id, exc)
