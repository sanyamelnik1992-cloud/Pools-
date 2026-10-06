"""Прогон одной стратегии по ряду цен и дохода — общий для проверки на истории и сценариев."""
from __future__ import annotations

from suibot.book import Book, Costs, accrue_usd, check_stop, init_book, step, summary
from suibot.chain import PoolCfg, state_from_price
from suibot.rally import RallyWatch
from suibot.strategy import Strategy


def no_price(_t):
    return 0.0, 9


def simulate(s: Strategy, pc: PoolCfg, times: list[float], prices: list[float], yields: list[float],
             costs: Costs, spacing: int, scale: float = 1.0) -> dict:
    """yields[j] — доход единицы ликвидности за свечу j (доля стоимости полнодиапазонной позиции).
    Доход зачисляется, пока цена в диапазоне (по ценам начала и конца свечи); возвращает итог summary()."""
    def state(t, p):
        return dict(state_from_price(p, pc.a_is_sui), t=t, spacing=spacing)

    prev = state(times[0], prices[0])
    book: Book = init_book(s, pc, prev, costs, scale)
    stop = s.stop_vs_split_pct is not None
    watch = RallyWatch(s.rally_exit, drop_rules=s.crash_exit)
    watch.add(times[0], prices[0])
    for t, p, y in zip(times[1:], prices[1:], yields[1:]):
        st = state(t, p)
        k = (book.in_range(prev["sq"]) + book.in_range(st["sq"])) / 2
        usd = k * y * (st["ua"] / st["sq"] + st["ub"] * st["sq"]) * book.L if k and y else 0.0
        accrue_usd(book, k, usd, st, t - prev["t"])
        step(book, s, st, watch, costs)
        if stop:
            check_stop(book, s, summary(book, st, no_price))
        prev = st
    return summary(book, prev, no_price)
