#!/usr/bin/env python3
"""Бот-ребалансер пулов SUI/USDC на Cetus.

  python3 bot.py paper [--reset]            бумажный режим: виртуальные позиции на живых данных пула
  python3 bot.py report                     отчёт по бумажным позициям на текущих ценах
  python3 bot.py backtest [--days 30] [--minutes 5]
                                            тестовый режим: те же стратегии на истории

Стратегии и издержки — в suibot.toml (можно указать другой файл: --config). Состояние и журналы —
data/private/bot/ (state.json, events.csv, snapshots.csv; в git не попадают). Уведомления в Telegram —
если заданы переменные окружения TG_TOKEN и TG_CHAT. Ключей и денег бот не использует.
"""
from __future__ import annotations

import argparse

from lpscan.common import ROOT
from suibot import backtest
from suibot.config import load
from suibot.paper import Paper


def main():
    ap = argparse.ArgumentParser(description="Бот-ребалансер SUI/USDC на Cetus")
    ap.add_argument("mode", choices=["paper", "report", "backtest"])
    ap.add_argument("--config", default=str(ROOT / "suibot.toml"))
    ap.add_argument("--reset", action="store_true", help="paper: начать заново, удалив сохранённое состояние")
    ap.add_argument("--ticks", type=int, help="paper: сделать N опросов и выйти (для проверки)")
    ap.add_argument("--days", type=int, default=30, help="backtest: глубина истории в днях")
    ap.add_argument("--minutes", type=int, default=5, help="backtest: размер свечи в минутах (5, 15, 60)")
    a = ap.parse_args()
    cfg = load(a.config)
    if a.mode == "paper":
        Paper(cfg, reset=a.reset).run(a.ticks)
    elif a.mode == "report":
        Paper(cfg).show()
    else:
        backtest.run(cfg, a.days, a.minutes)


if __name__ == "__main__":
    main()
