"""Уведомления в Telegram — если заданы переменные окружения TG_TOKEN (токен бота) и TG_CHAT (id чата)."""
from __future__ import annotations

import os

import requests


def send(text: str):
    token, chat = os.environ.get("TG_TOKEN"), os.environ.get("TG_CHAT")
    if not token or not chat:
        return
    try:
        requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                      json={"chat_id": chat, "text": text}, timeout=15)
    except requests.RequestException:
        pass   # уведомление не должно ронять бота


def commands(offset: int | None) -> tuple[list[str], int | None]:
    """Новые команды из Telegram (сообщения, начинающиеся с «/») — только из своего чата TG_CHAT."""
    token, chat = os.environ.get("TG_TOKEN"), os.environ.get("TG_CHAT")
    if not token or not chat:
        return [], offset
    try:
        r = requests.get(f"https://api.telegram.org/bot{token}/getUpdates",
                         params={"offset": offset, "timeout": 0}, timeout=15).json()
    except (requests.RequestException, ValueError):
        return [], offset
    out = []
    for u in r.get("result", []):
        offset = u["update_id"] + 1
        m = u.get("message") or {}
        text = (m.get("text") or "").strip()
        if str(m.get("chat", {}).get("id")) == str(chat) and text.startswith("/"):
            out.append(text[1:].split("@")[0].split()[0].lower())
    return out, offset
