"""История для тестового режима и сценариев: свечи цены SUI и доход единицы ликвидности пулов.

Цена — 5-минутные (или другие) свечи SUI/USDT из открытого архива Binance (полная история; биржевая цена
SUI и цена в пулах выравниваются арбитражем) либо свечи самого пула GeckoTerminal (только ~6 месяцев).
Доход — фактический дневной доход единицы ликвидности пула из счётчиков (sui_pools.pool_history),
разнесённый по свечам пропорционально объёму торгов.
"""
from __future__ import annotations

import bisect
import time

from lpscan.common import get_json
from sui_pools import daily_checkpoints, gecko_candles, pool_history
from suibot.chain import PoolCfg

BINANCE = "https://data-api.binance.vision/api/v3/klines"
INTERVALS = {1: "1m", 5: "5m", 15: "15m", 30: "30m", 60: "1h", 1440: "1d"}


def binance_candles(start: float, end: float, minutes: int = 5) -> list[tuple[float, float, float]]:
    """Свечи SUIUSDT: начало свечи (с), цена закрытия, объём в USDT."""
    out, t, step = [], int(start * 1000), minutes * 60_000
    while t < end * 1000:
        closed = t / 1000 + 1000 * minutes * 60 < time.time() - 86400   # закрытые куски кэшируем надолго
        rows = get_json(BINANCE, params={"symbol": "SUIUSDT", "interval": INTERVALS[minutes], "startTime": t,
                                         "limit": 1000}, cache_ttl=30 * 86400 if closed else 600, min_interval=0.2)
        if not rows:
            break
        out += [(r[0] / 1000, float(r[4]), float(r[7])) for r in rows]
        t = rows[-1][0] + step
    return [c for c in out if c[0] <= end]


def trend_warmup(days: float, end: float) -> list[tuple[float, float]]:
    """Часовые цены SUIUSDT за days (+1) дней до end — предыстория для средней фильтра тренда."""
    return [(c[0], c[1]) for c in binance_candles(end - (days + 1) * 86400, end - 3600, 60)]


def daily_closes(days: int) -> list[tuple[float, float]]:
    """Дневные цены закрытия SUIUSDT за последние days дней: [(начало дня, цена)]."""
    end = time.time()
    return [(c[0], c[1]) for c in binance_candles(end - days * 86400, end, 1440)]


def trend_days(strategies) -> float:
    """Сколько дней предыстории нужно стратегиям: окно средней фильтра тренда, для фазы рынка — два окна
    (чтобы фаза успела установиться); 0 — ни фильтра, ни фаз."""
    return max((max(s.trend_ma_days or 0, 2 * (s.phase_ma_days or 0)) for s in strategies), default=0)


def load(pools: dict[str, PoolCfg], days: int, minutes: int = 5, source: str = "binance"):
    """Одни свечи и доход единицы ликвидности каждого пула на каждую свечу (доля полного диапазона):
    возвращает (candles, {пул: [y по свечам]})."""
    cps = daily_checkpoints(days)
    hist = {k: pool_history(pc.object, pc.dex, pc.a_is_sui, cps)[0] for k, pc in pools.items()}
    first = next(iter(hist.values()))
    t0, t1 = first[0]["t"], first[-1]["t"]
    if source == "binance":
        cs = binance_candles(t0, t1, minutes)
    else:
        pc = next(iter(pools.values()))
        cs = [c for c in gecko_candles(pc.object, pc.a_is_sui, t0, minutes) if t0 <= c[0] <= t1]
    yields = {}
    for k, pts in hist.items():
        day_t = [p["t"] for p in pts]
        idx = [bisect.bisect_right(day_t, c[0]) - 1 for c in cs]
        vol: dict[int, float] = {}
        for i, c in zip(idx, cs):
            vol[i] = vol.get(i, 0.0) + c[2]
        yields[k] = [(pts[i]["y_fee"] + pts[i]["y_rew"]) * (c[2] / vol[i] if vol[i] else minutes * 60 / 86400)
                     if 0 <= i < len(pts) - 1 else 0.0 for i, c in zip(idx, cs)]
    return cs, yields
