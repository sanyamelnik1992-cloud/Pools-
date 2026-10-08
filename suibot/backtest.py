"""Тестовый режим: те же стратегии и тот же учёт, что в бумажном режиме, но на истории.

Цена — 5-минутные свечи (на часовых узкие диапазоны выглядят лучше, чем есть: не видно выходов цены внутри
часа); доход — фактический доход единицы ликвидности пула (suibot.history). Диапазон из initial_range
пересчитывается к цене начала истории в той же пропорции к текущей цене.
"""
from __future__ import annotations

from suibot import history, report
from suibot.chain import read_pools
from suibot.config import Config
from suibot.sim import simulate


def run(cfg: Config, days: int = 30, minutes: int = 5, source: str = "binance") -> list[dict]:
    pools = cfg.pools_used()
    now = read_pools(pools)
    cs, yields = history.load(pools, days, minutes, source)
    times, prices = [c[0] for c in cs], [c[1] for c in cs]
    td = history.trend_days(cfg.strategies)
    warm = history.trend_warmup(td, times[0]) if td else None
    rows = [simulate(s, cfg.pools[s.pool], times, prices, yields[s.pool], cfg.costs, now[s.pool]["spacing"],
                     scale=prices[0] / now[s.pool]["sui"], warm=warm) for s in cfg.strategies]
    print(f"История: {days} дней, свечи {minutes} мин ({source}), SUI ${prices[0]:.4f} → ${prices[-1]:.4f} "
          f"({prices[-1] / prices[0] - 1:+.0%})\n")
    print(report.table(rows, cfg.staking_apy))
    return rows
