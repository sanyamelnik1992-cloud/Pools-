"""Сценарии будущего: цена SUI меняется в multiple раз за days дней (например, ×2 за 60 дней).

Пути строятся блочным бутстрепом из реальной истории: случайные куски по block_days суток (5-минутные
изменения цены вместе с доходом единицы ликвидности за те же свечи) — так сохраняются всплески
волатильности, многодневные тренды и то, что комиссии растут вместе с волатильностью. К изменениям
добавляется постоянный наклон, чтобы каждый путь закончился ровно в multiple раз выше старта.
Каждая стратегия проходит одни и те же пути.
"""
from __future__ import annotations

import math
import random
import statistics as st

from suibot import history
from suibot.chain import read_pools
from suibot.config import Config
from suibot.sim import simulate


def _q(v: list[float], f: float) -> float:
    v = sorted(v)
    return v[min(len(v) - 1, int(f * (len(v) - 1) + 0.5))]


def make_paths(cs: list, yields: dict, p0: float, multiple: float, days: int, paths: int, minutes: int = 5,
               block_days: int = 5, seed: int = 1):
    """Пути цены из случайных кусков истории (вместе с доходом пулов за те же свечи), каждый ровно ×multiple:
    выдаёт (times, prices, {пул: доход по свечам})."""
    rets = [math.log(b[1] / a[1]) for a, b in zip(cs, cs[1:])]
    per_day = 1440 // minutes
    block = per_day * block_days                       # длинные блоки сохраняют многодневные тренды
    blocks = range(0, len(rets) - block, per_day)
    steps = days * per_day
    drift = math.log(multiple) / steps
    rng = random.Random(seed)
    times = [j * minutes * 60.0 for j in range(steps + 1)]
    for _ in range(paths):
        picks = [rng.choice(blocks) for _ in range(-(-days // block_days))]
        idx = [b + j for b in picks for j in range(block)][:steps]
        r = [rets[i] for i in idx]
        adj = drift - sum(r) / steps                       # путь заканчивается ровно на ×multiple
        prices, lp = [p0], math.log(p0)
        for x in r:
            lp += x + adj
            prices.append(math.exp(lp))
        yield times, prices, {k: [0.0] + [y[i + 1] for i in idx] for k, y in yields.items()}


def run(cfg: Config, multiple: float = 2.0, days: int = 60, paths: int = 100, minutes: int = 5,
        hist_days: int = 180, block_days: int = 5, seed: int = 1) -> dict:
    pools = cfg.pools_used()
    now = read_pools(pools)
    cs, yields = history.load(pools, hist_days, minutes, "binance")
    p0 = next(iter(now.values()))["sui"]
    res = {s.name: [] for s in cfg.strategies}
    for times, prices, ys in make_paths(cs, yields, p0, multiple, days, paths, minutes, block_days, seed):
        for s in cfg.strategies:
            res[s.name].append(simulate(s, cfg.pools[s.pool], times, prices, ys[s.pool], cfg.costs,
                                        now[s.pool]["spacing"]))
    out = {"multiple": multiple, "days": days, "paths": paths, "block_days": block_days, "start_price": p0,
           "strategies": {}}
    for name, rows in res.items():
        sui = [r["value_sui"] for r in rows]
        out["strategies"][name] = {
            "capital_sui": rows[0]["capital_sui"], "sui_median": st.median(sui), "sui_p10": _q(sui, 0.1),
            "sui_p90": _q(sui, 0.9), "value_median": st.median(r["value"] for r in rows),
            "vs_split_median": st.median(r["vs_split"] for r in rows), "fees_median": st.median(r["fees_usd"] for r in rows),
            "rebalances_median": st.median(r["rebalances"] for r in rows),
            "in_range_median": st.median(r["in_range_pct"] for r in rows),
            "exited_share": sum(r["exits"] > 0 for r in rows) / len(rows),
            "sui_amount_median": st.median(r["sui_amount"] for r in rows),
            "usdc_amount_median": st.median(r["usdc_amount"] for r in rows),
            "split": (rows[0]["split_sui"], rows[0]["split_usdc"]),
            "vs_split_share": sum(r["vs_split"] > 0 for r in rows) / len(rows)}
    print(table(out, cfg.staking_apy))
    print("\n" + split_table(out))
    return out


def table(out: dict, staking_apy: float) -> str:
    m, d = out["multiple"], out["days"]
    lines = [f"Сценарий: SUI ×{m:g} за {d} дней (${out['start_price']:.3f} → ${out['start_price'] * m:.3f}), "
             f"{out['paths']} путей из реальной истории (куски по {out['block_days']} дн.)\n",
             f"{'Стратегия':28s} {'SUI в конце: медиана':>21s} {'10%–90%':>15s} {'К холду, SUI':>13s} "
             f"{'Стоимость':>10s} {'Комиссии':>9s} {'Пересб.':>7s} {'В диап.':>7s} {'Выход в SUI':>12s}"]
    for name, r in out["strategies"].items():
        lines.append(f"{name[:28]:28s} {r['sui_median']:>21,.0f} {r['sui_p10']:>7,.0f}–{r['sui_p90']:<7,.0f} "
                     f"{r['sui_median'] - r['capital_sui']:>+13,.0f} ${r['value_median']:>9,.0f} ${r['fees_median']:>8,.0f} "
                     f"{r['rebalances_median']:>7.0f} {r['in_range_median']:>6.0f}% {r['exited_share']:>11.0%}")
    cap = next(iter(out["strategies"].values()))["capital_sui"]
    lines.append(f"{'просто держать SUI':28s} {cap:>21,.0f} {'':15s} {0:>+13,.0f} ${cap * out['start_price'] * m:>9,.0f}")
    stake = cap * (1 + staking_apy * d / 365)
    lines.append(f"{'стейкинг SUI':28s} {stake:>21,.0f} {'':15s} {stake - cap:>+13,.0f} "
                 f"${stake * out['start_price'] * m:>9,.0f}")
    return "\n".join(lines)


def split_table(out: dict) -> str:
    """Что лежит в конце (SUI и USDC) против «обменять ту же долю на USDC и держать»: на росте пул копит USDC,
    на падении — SUI; вклад самого пула — разница в стоимости."""
    p = out["start_price"] * out["multiple"]
    lines = [f"{'Стратегия':28s} {'В конце: SUI + USDC':>22s} {'Та же доля: SUI + USDC':>24s} "
             f"{'Пул к той же доле':>18s} {'в SUI':>7s} {'лучше в':>8s}"]
    for name, r in out["strategies"].items():
        ss, su = r["split"]
        lines.append(f"{name[:28]:28s} {r['sui_amount_median']:>9,.0f} + ${r['usdc_amount_median']:>9,.0f} "
                     f"{ss:>10,.0f} + ${su:>9,.0f} {r['vs_split_median']:>+17,.0f}$ {r['vs_split_median'] / p:>+7,.0f} "
                     f"{r['vs_split_share']:>7.0%}")
    return "\n".join(lines)
