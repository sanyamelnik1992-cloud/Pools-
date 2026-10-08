#!/usr/bin/env python3
"""Бот-ребалансер пулов SUI/USDC на Cetus.

  python3 bot.py paper [--reset]            бумажный режим: виртуальные позиции на живых данных пула
  python3 bot.py report                     отчёт по бумажным позициям на текущих ценах
  python3 bot.py backtest [--days 30] [--minutes 5] [--source binance|gecko]
                                            тестовый режим: те же стратегии на истории
  python3 bot.py scenario [--multiple 2] [--days 60] [--paths 100]
                                            сценарий будущего: цена ×multiple за days дней, пути из реальной истории
  python3 bot.py optimize [--paths 30]      подбор стратегии: перебор параметров на кварталах года и сценариях
  python3 bot.py phases [--grid]            стратегии на всей истории SUI с 2023 (бычьи рывки, спады, медвежий год)
                                            и хронология фаз рынка; --grid — боевая стратегия и её соседи
  python3 bot.py live                       боевой режим: стратегия из [live] на реальном кошельке
                                            (dry_run = true в suibot.toml — только симуляция)
  python3 bot.py control <команда>          ручное управление работающим ботом: status, pause, resume, sui, usdc, close
  python3 bot.py status                     состояние боевого режима и последние события (только чтение)
  python3 bot.py check                      проверка перед запуском: .env, Telegram, кошелёк, настройки

Стратегии и издержки — в suibot.toml (можно указать другой файл: --config). Состояние и журналы —
data/private/bot/ (в git не попадают). Уведомления и команды в Telegram — если заданы переменные окружения
TG_TOKEN и TG_CHAT. Ключ кошелька нужен только боевому режиму: переменная SUI_PRIVATE_KEY (suiprivkey1…).
Боевой режим берёт их и из файла .env в папке проекта (другой файл: --env, например на сервере); см. .env.example.
"""
from __future__ import annotations

import argparse
from pathlib import Path

from lpscan.common import ROOT
from suibot import backtest, env, optimize, phases, scenario
from suibot.config import load
from suibot.live import Live, check, control, show
from suibot.paper import Paper


def main():
    ap = argparse.ArgumentParser(description="Бот-ребалансер SUI/USDC на Cetus")
    ap.add_argument("mode", choices=["paper", "report", "backtest", "scenario", "optimize", "phases", "live", "control",
                                          "status", "check"])
    ap.add_argument("command", nargs="?", help="control: status | pause | resume | sui | usdc | close")
    ap.add_argument("--config", default=str(ROOT / "suibot.toml"))
    ap.add_argument("--env", default=str(ROOT / ".env"), help="live, check: файл с SUI_PRIVATE_KEY, TG_TOKEN, TG_CHAT")
    ap.add_argument("--reset", action="store_true", help="paper: начать заново, удалив сохранённое состояние")
    ap.add_argument("--ticks", type=int, help="paper: сделать N опросов и выйти (для проверки)")
    ap.add_argument("--days", type=int, help="backtest: глубина истории (30); scenario: длина сценария (60)")
    ap.add_argument("--minutes", type=int, default=5, help="размер свечи в минутах (5, 15, 60)")
    ap.add_argument("--source", choices=["binance", "gecko"], default="binance",
                    help="backtest: цены Binance SUIUSDT (полная история) или свечи пула GeckoTerminal (~6 мес.)")
    ap.add_argument("--multiple", type=float, default=2.0, help="scenario: во сколько раз изменится цена")
    ap.add_argument("--paths", type=int, default=100, help="scenario: число путей")
    ap.add_argument("--hist-days", type=int, default=180, help="scenario: из какой истории брать куски для путей")
    ap.add_argument("--block-days", type=int, default=5, help="scenario: длина куска истории в сутках")
    ap.add_argument("--grid", action="store_true", help="phases: боевая стратегия и её соседние настройки")
    a = ap.parse_args()
    cfg = load(a.config)
    if a.mode == "paper":
        Paper(cfg, reset=a.reset).run(a.ticks)
    elif a.mode == "report":
        Paper(cfg).show()
    elif a.mode == "backtest":
        backtest.run(cfg, a.days or 30, a.minutes, a.source)
    elif a.mode == "scenario":
        scenario.run(cfg, a.multiple, a.days or 60, a.paths, a.minutes, a.hist_days, a.block_days)
    elif a.mode == "optimize":
        optimize.run(cfg, a.paths if a.paths != 100 else 30)
    elif a.mode == "phases":
        phases.run_all(cfg, a.grid)
    elif a.mode == "live":
        names = env.load(Path(a.env))              # только боевой режим загружает ключ; значения не печатаются
        if names:
            print(f"из {a.env} загружены: {', '.join(sorted(names))}")
        Live(cfg).run(a.ticks)
    elif a.mode == "check":
        env.load(Path(a.env))                     # ключ загружается, но не печатается
        check(cfg, a.env)
    elif a.mode == "status":
        show(cfg)
    else:
        control(cfg, (a.command or "").lower())


if __name__ == "__main__":
    main()
