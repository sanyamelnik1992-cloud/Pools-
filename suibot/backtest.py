"""Тестовый режим: те же стратегии и тот же учёт, что в бумажном режиме, но на истории.

Цена — свечи пула GeckoTerminal (по умолчанию 5 минут: на часовых свечах узкие диапазоны выглядят лучше,
чем есть, потому что не видно выходов цены внутри часа). Доход — фактический дневной доход единицы
ликвидности из счётчиков пула (sui_pools.pool_history), разнесённый по свечам пропорционально объёму.
Диапазон из initial_range пересчитывается к цене начала истории в той же пропорции к текущей цене.
"""
from __future__ import annotations

import bisect

from sui_pools import daily_checkpoints, gecko_candles, pool_history
from suibot import report
from suibot.book import accrue_usd, check_stop, init_book, rebalance, summary, update_out
from suibot.chain import read_pools, state_from_price
from suibot.config import Config


def run(cfg: Config, days: int = 30, minutes: int = 5) -> list[dict]:
    pools = cfg.pools_used()
    now = read_pools(pools)
    data = {}
    for key, pc in pools.items():
        pts, _ = pool_history(pc.object, pc.dex, pc.a_is_sui, daily_checkpoints(days))
        cs = [c for c in gecko_candles(pc.object, pc.a_is_sui, pts[0]["t"], minutes) if pts[0]["t"] <= c[0] <= pts[-1]["t"]]
        day_t = [p["t"] for p in pts]
        idx = [bisect.bisect_right(day_t, c[0]) - 1 for c in cs]
        vol: dict[int, float] = {}
        for i, c in zip(idx, cs):
            vol[i] = vol.get(i, 0.0) + c[2]
        data[key] = (pts, cs, idx, vol)
    rows = []
    for s in cfg.strategies:
        pc = cfg.pools[s.pool]
        pts, cs, idx, vol = data[s.pool]
        spacing = now[s.pool]["spacing"]

        def state(t, price, pc=pc, spacing=spacing):
            return dict(state_from_price(price, pc.a_is_sui), t=t, spacing=spacing)

        prev = state(cs[0][0], cs[0][1])
        book = init_book(s, pc, prev, cfg.costs, scale=cs[0][1] / now[s.pool]["sui"])
        for (t, price, v), i in zip(cs[1:], idx[1:]):
            st = state(t, price)
            k = (book.in_range(prev["sq"]) + book.in_range(st["sq"])) / 2
            usd = 0.0
            if k and 0 <= i < len(pts) - 1:
                share = v / vol[i] if vol[i] else minutes * 60 / 86400
                full_per_l = st["ua"] / st["sq"] + st["ub"] * st["sq"]   # $ полного диапазона на ед. ликвидности
                usd = k * (pts[i]["y_fee"] + pts[i]["y_rew"]) * share * full_per_l * book.L
            accrue_usd(book, k, usd, st, t - prev["t"])
            update_out(book, st)
            if s.rebalance_reason(book, price, t):
                rebalance(book, st, s, cfg.costs)
            check_stop(book, s, summary(book, st, lambda _t: (0.0, 9)))
            prev = st
        rows.append(summary(book, prev, lambda _t: (0.0, 9)))
    first = data[cfg.strategies[0].pool][1]
    print(f"История: {days} дней, свечи {minutes} мин, SUI ${first[0][1]:.4f} → ${first[-1][1]:.4f} "
          f"({first[-1][1] / first[0][1] - 1:+.0%})\n")
    print(report.table(rows))
    return rows
