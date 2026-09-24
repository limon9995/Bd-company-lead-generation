"""Telegram (default) and WhatsApp Cloud API notifications."""
import html
import logging

import httpx
from sqlalchemy.orm import Session

from app import settings_store
from app.services.errors import ProviderError

log = logging.getLogger(__name__)
TG = "https://api.telegram.org/bot{token}/{method}"


def _tg(token: str, method: str, **params) -> dict:
    try:
        r = httpx.post(TG.format(token=token, method=method), json=params, timeout=20)
    except httpx.HTTPError as exc:
        raise ProviderError(f"Telegram request failed: {exc}") from exc
    data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    if not data.get("ok"):
        raise ProviderError(f"Telegram {method} failed: {data.get('description') or r.text[:200]}")
    return data


def telegram_get_me(token: str) -> dict:
    return _tg(token, "getMe")["result"]


def telegram_detect_chats(token: str) -> list[dict]:
    """Chats that recently messaged the bot (user must send /start first)."""
    updates = _tg(token, "getUpdates", timeout=0, allowed_updates=["message", "channel_post", "my_chat_member"])["result"]
    chats: dict[int, dict] = {}
    for u in updates:
        msg = u.get("message") or u.get("channel_post") or u.get("my_chat_member") or {}
        chat = msg.get("chat") or {}
        if "id" in chat:
            name = chat.get("title") or " ".join(filter(None, [chat.get("first_name"), chat.get("last_name")])) or chat.get("username", "")
            chats[chat["id"]] = {"chat_id": str(chat["id"]), "type": chat.get("type", ""), "name": name,
                                 "username": chat.get("username", "")}
    return list(chats.values())


def telegram_send(token: str, chat_id: str, text: str) -> None:
    for i in range(0, len(text), 4000):
        _tg(token, "sendMessage", chat_id=chat_id, text=text[i:i + 4000], parse_mode="HTML",
            disable_web_page_preview=True)


def whatsapp_send(s: dict, text: str) -> None:
    url = f"https://graph.facebook.com/{s['whatsapp_api_version']}/{s['whatsapp_phone_number_id']}/messages"
    payload = {
        "messaging_product": "whatsapp", "to": s["whatsapp_to"], "type": "template",
        "template": {"name": s["whatsapp_template"], "language": {"code": s["whatsapp_template_lang"]},
                     "components": [{"type": "body", "parameters": [{"type": "text", "text": text[:1000]}]}]},
    }
    try:
        r = httpx.post(url, json=payload, headers={"Authorization": f"Bearer {s['whatsapp_token']}"}, timeout=20)
    except httpx.HTTPError as exc:
        raise ProviderError(f"WhatsApp request failed: {exc}") from exc
    if r.status_code >= 300:
        raise ProviderError(f"WhatsApp {r.status_code}: {r.text[:300]}")


def telegram_ready(db: Session) -> bool:
    return settings_store.is_set(db, "telegram_bot_token") and bool(settings_store.get(db, "telegram_chat_id"))


def whatsapp_ready(db: Session) -> bool:
    return all(bool(settings_store.get(db, k)) for k in
               ("whatsapp_token", "whatsapp_phone_number_id", "whatsapp_to", "whatsapp_template"))


def notify(db: Session, html_text: str, plain_text: str | None = None) -> list[str]:
    """Send to every configured channel. Never raises; returns a list of error strings."""
    errors: list[str] = []
    if telegram_ready(db):
        try:
            telegram_send(settings_store.get(db, "telegram_bot_token"), settings_store.get(db, "telegram_chat_id"), html_text)
        except ProviderError as exc:
            errors.append(str(exc))
    if whatsapp_ready(db):
        try:
            whatsapp_send(settings_store.snapshot(db), plain_text or strip_html(html_text))
        except ProviderError as exc:
            errors.append(str(exc))
    for e in errors:
        log.warning("notify: %s", e)
    return errors


def strip_html(s: str) -> str:
    import re

    return html.unescape(re.sub(r"<[^>]+>", "", s))


def esc(s) -> str:
    return html.escape(str(s or ""), quote=False)
