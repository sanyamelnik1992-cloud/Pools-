"""Секреты из файла .env (для Mac и сервера): ключ кошелька и Telegram без ручного ввода в терминале.

Формат — строки `ИМЯ=значение`, можно с `export ` в начале и в кавычках (тогда тот же файл годится для
`source .env`); `#` — комментарий. Уже заданные переменные окружения не перезаписываются. Принимаются и
привычные имена: PRIVATE_KEY → SUI_PRIVATE_KEY, BOT_TOKEN → TG_TOKEN, CHAT_ID → TG_CHAT.

Значения нигде не печатаются. Файл читает только боевой режим (python3 bot.py live): status, control,
backtest и прочие режимы ключ в память не загружают. Права на файл сужаются до 600 (только владелец).
"""
from __future__ import annotations

import os
import stat
from pathlib import Path

ALIASES = {"PRIVATE_KEY": "SUI_PRIVATE_KEY", "BOT_TOKEN": "TG_TOKEN", "CHAT_ID": "TG_CHAT",
           "TELEGRAM_TOKEN": "TG_TOKEN", "TELEGRAM_CHAT_ID": "TG_CHAT"}


def parse(text: str) -> dict[str, str]:
    out = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        name, sep, value = line.partition("=")
        name, value = name.strip(), value.strip()
        if not sep or not name:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        elif " #" in value:                            # комментарий после значения без кавычек
            value = value.split(" #", 1)[0].rstrip()
        out[ALIASES.get(name, name)] = value
    return out


def load(path: Path) -> list[str]:
    """Загрузить .env в окружение процесса; вернуть имена загруженных переменных (без значений)."""
    if not path.is_file():                             # нет файла или --env /dev/null — запуск без секретов
        return []
    if os.name == "posix" and path.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        try:
            path.chmod(0o600)                          # ключ не должны читать другие пользователи системы
        except OSError:
            pass
    loaded = []
    for name, value in parse(path.read_text()).items():
        if value and not os.environ.get(name):
            os.environ[name] = value
            loaded.append(name)
    return loaded
