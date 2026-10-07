"""Уведомления в Telegram — если заданы переменные окружения TG_TOKEN (токен бота) и TG_CHAT (id чата)."""
from __future__ import annotations

import os
import re
import time
from html import unescape

import requests

STALE_SECONDS = 600       # команды старше 10 минут пропускаются
ARG_COMMANDS = {"alert"}  # команды с аргументом: «/alert 1.30» приходит боту как «alert 1.30»


def send(text: str, html: bool = False, buttons: list | None = None) -> str | None:
    """Отправить сообщение (html=True — с разметкой <b>, <a>, <code>; buttons — кнопки под сообщением:
    [[(надпись, команда), ...], ...]); вернуть текст ошибки или None, если дошло.
    Если Telegram не принял разметку, сообщение уходит простым текстом. Бота ошибка не роняет."""
    token, chat = os.environ.get("TG_TOKEN"), os.environ.get("TG_CHAT")
    if not token or not chat:
        return "не заданы TG_TOKEN и TG_CHAT"
    body = {"chat_id": chat, "text": text[:4000], "disable_web_page_preview": True}
    if html:
        body["parse_mode"] = "HTML"
    if buttons:
        body["reply_markup"] = {"inline_keyboard": [[{"text": t, "callback_data": c} for t, c in row] for row in buttons]}
    try:
        r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage", json=body, timeout=15)
        if not r.ok and html:
            return send(unescape(re.sub(r"<[^>]+>", "", text)), buttons=buttons)
        return None if r.ok else f"Telegram ответил {r.status_code}: {r.json().get('description', '')}"
    except (requests.RequestException, ValueError) as e:
        return f"нет связи с Telegram: {type(e).__name__}"


def set_menu(commands: dict[str, str]):
    """Меню команд в Telegram (кнопка «/» рядом с полем ввода)."""
    token = os.environ.get("TG_TOKEN")
    if not token:
        return
    try:
        requests.post(f"https://api.telegram.org/bot{token}/setMyCommands", timeout=15,
                      json={"commands": [{"command": c, "description": d} for c, d in commands.items()]})
    except requests.RequestException:
        pass


def commands(offset: int | None) -> tuple[list[str], int | None]:
    """Новые команды из Telegram (сообщения, начинающиеся с «/», и нажатия кнопок под сообщениями) — только из
    личного чата TG_CHAT и только от его владельца (в личном чате id чата совпадает с id пользователя)."""
    token, chat = os.environ.get("TG_TOKEN"), os.environ.get("TG_CHAT")
    if not token or not chat:
        return [], offset
    try:
        r = requests.get(f"https://api.telegram.org/bot{token}/getUpdates",
                         params={"offset": offset, "timeout": 0}, timeout=15).json()
    except (requests.RequestException, ValueError):
        return [], offset
    out = []
    for u in r.get("result") or []:
        try:
            offset = int(u["update_id"]) + 1
            cq = u.get("callback_query")
            if cq:                                    # нажата кнопка под сообщением бота
                if str(cq.get("from", {}).get("id")) == str(chat) and \
                        str((cq.get("message") or {}).get("chat", {}).get("id")) == str(chat):
                    answer(token, cq.get("id"))
                    if (cq.get("data") or "").strip():
                        out.append(cq["data"].strip().lower())
                continue
            m = u.get("message") or {}
            text = (m.get("text") or "").strip()
            if str(m.get("chat", {}).get("id")) != str(chat) or str(m.get("from", {}).get("id")) != str(chat):
                continue
            if m.get("date") and time.time() - float(m["date"]) > STALE_SECONDS:
                continue   # команда, отправленная, пока бот не работал, — не выполняется
            words = text[1:].split() if text.startswith("/") else []
            name = words[0].split("@")[0].lower() if words else ""
            if name:
                out.append(" ".join([name] + words[1:2]) if name in ARG_COMMANDS else name)
        except (KeyError, TypeError, ValueError, AttributeError):
            continue   # странное сообщение не должно ронять бота
    return out, offset


def answer(token: str, callback_id: str | None):
    """Убрать «часики» на нажатой кнопке."""
    try:
        requests.post(f"https://api.telegram.org/bot{token}/answerCallbackQuery", json={"callback_query_id": callback_id},
                      timeout=10)
    except requests.RequestException:
        pass


def send_file(path, caption: str = "", photo: bool = False) -> str | None:
    """Картинка (photo=True) или файл в чат; вернуть текст ошибки или None."""
    token, chat = os.environ.get("TG_TOKEN"), os.environ.get("TG_CHAT")
    if not token or not chat:
        return "не заданы TG_TOKEN и TG_CHAT"
    method, field = ("sendPhoto", "photo") if photo else ("sendDocument", "document")
    try:
        with open(path, "rb") as f:
            r = requests.post(f"https://api.telegram.org/bot{token}/{method}", timeout=60,
                              data={"chat_id": chat, "caption": caption[:1000], "parse_mode": "HTML"},
                              files={field: f})
        return None if r.ok else f"Telegram ответил {r.status_code}"
    except (OSError, requests.RequestException) as e:
        return f"не удалось отправить файл: {type(e).__name__}"
