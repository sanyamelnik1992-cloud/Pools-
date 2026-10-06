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
