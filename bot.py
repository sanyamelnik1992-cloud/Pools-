#!/usr/bin/env python3
"""Бот-ребалансер пулов SUI/USDC на Cetus.

  python3 bot.py paper [--reset]            бумажный режим: виртуальные позиции на живых данных пула
  python3 bot.py report                     отчёт по бумажным позициям на текущих ценах
  python3 bot.py backtest [--days 30] [--minutes 5] [--source binance|gecko]
                                            тестовый режим: те же стратегии на истории
  python3 bot.py scenario [--multiple 2] [--days 60] [--paths 100]
                                            сценарий будущего: цена ×multiple за days дней, пути из реальной истории

Стратегии и издержки — в suibot.toml (можно указать другой файл: --config). Состояние и журналы —
data/private/bot/ (state.json, events.csv, snapshots.csv; в git не попадают). Уведомления в Telegram —
если заданы переменные окружения TG_TOKEN и TG_CHAT. Ключей и денег бот не использует.
"""
from __future__ import annotations

import argparse

from lpscan.common import ROOT
from suibot import backtest, scenario
from suibot.config import load
from suibot.paper import Paper


def main():
    ap = argparse.ArgumentParser(description="Бот-ребалансер SUI/USDC на Cetus")
    ap.add_argument("mode", choices=["paper", "report", "backtest", "scenario"])
    ap.add_argument("--config", default=str(ROOT / "suibot.toml"))
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
    a = ap.parse_args()
    cfg = load(a.config)
    if a.mode == "paper":
        Paper(cfg, reset=a.reset).run(a.ticks)
    elif a.mode == "report":
        Paper(cfg).show()
    elif a.mode == "backtest":
        backtest.run(cfg, a.days or 30, a.minutes, a.source)
    else:
        scenario.run(cfg, a.multiple, a.days or 60, a.paths, a.minutes, a.hist_days, a.block_days)


if __name__ == "__main__":
    main()
