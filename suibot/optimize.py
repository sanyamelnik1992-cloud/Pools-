"""Подбор стратегии перебором параметров — цель: рост капитала, устойчивый к направлению рынка.

Каждая стратегия проходит одни и те же тесты:
  • четыре квартала последнего года на реальных 5-минутных ценах (обвал, боковики, рост — разные режимы рынка);
  • реальный рывок последних 60 дней, растянутый до ×2;
  • сценарии на 60 дней: рост ×2, цена на месте, падение ×0.5 (пути из кусков реальной истории, медиана по путям).
Метрики теста — рост капитала в $ к началу, результат к холду SUI и к «той же доле» (обменять ту же долю SUI на
USDC и держать — то, что добавляют сам пул и правила выхода). Оценка — средний результат к «той же доле» по всем
тестам; худший тест показан отдельно, чтобы выбирать устойчивую стратегию, а не самую удачную на одном отрезке.

Запуск:  python3 bot.py optimize [--paths 30]  →  data/processed/sui_optimize.csv
"""
from __future__ import annotations

import itertools
import math
import multiprocessing as mp
import statistics as st
import time

from analyze import _csv
from lpscan.common import PROC
from suibot import history
from suibot.chain import read_pools
from suibot.config import Config
from suibot.scenario import make_paths
from suibot.sim import simulate
from suibot.strategy import Strategy

WIDTHS = (0.03, 0.05, 0.08, 0.12)
OUT_MINUTES = (30, 180)
RALLY = (None, ([[72, 0.15]], 0.10), ([[72, 0.15]], 0.15), ([[72, 0.20]], 0.10))      # выход в SUI, возврат
CRASH = (None, ([[72, 0.15]], 0.10), ([[72, 0.20]], 0.10))                            # выход в USDC, возврат
CAPITAL_USD = 200
D: dict = {}          # данные тестов (общие для процессов)


def grid(pools: list[str], capital_sui: float) -> list[Strategy]:
    out = []
    for w, pool, om, rally, crash in itertools.product(WIDTHS, pools, OUT_MINUTES, RALLY, CRASH):
        name = (f"±{w * 100:g}% {pool} {om}м"
                + (f" | SUI +{rally[0][0][1]:.0%}/72ч, назад −{rally[1]:.0%}" if rally else "")
                + (f" | USDC −{crash[0][0][1]:.0%}/72ч, назад +{crash[1]:.0%}" if crash else ""))
        out.append(Strategy(name, pool, capital_sui, w, w, out_minutes=om,
                            rally_exit=rally[0] if rally else None, resume_drop_pct=rally[1] if rally else None,
                            crash_exit=crash[0] if crash else None, resume_rise_pct=crash[1] if crash else None))
    return out


def _metrics(s: Strategy, r: dict, p0: float) -> dict:
    v0 = s.capital_sui * p0
    return {"growth": r["value"] / v0 - 1, "vs_hold": r["vs_sui"] / v0, "vs_split": r["vs_split"] / v0}


def evaluate(s: Strategy) -> dict:
    pc, sp, costs, pnow = D["pcs"][s.pool], D["spacing"][s.pool], D["costs"], D["p_now"]
    cap = CAPITAL_USD / pnow
    tests = {}
    T, P, Y = D["T"], D["P"], D["Y"][s.pool]
    for name, (i0, i1) in D["quarters"].items():
        s_ = Strategy(**{**s.__dict__, "capital_sui": CAPITAL_USD / P[i0]})
        tests[name] = _metrics(s_, simulate(s_, pc, T[i0:i1], P[i0:i1], Y[i0:i1], costs, sp), P[i0])
    t, p, ys = D["replay"]
    s_ = Strategy(**{**s.__dict__, "capital_sui": cap})
    tests["рывок ×2"] = _metrics(s_, simulate(s_, pc, t, p, ys[s.pool], costs, sp), p[0])
    for m, paths in D["scen"].items():
        ms = [_metrics(s_, simulate(s_, pc, t, p, ys[s.pool], costs, sp), p[0]) for t, p, ys in paths]
        tests[f"сценарий ×{m:g}"] = {k: st.median(x[k] for x in ms) for k in ms[0]}
    vs = [x["vs_split"] for x in tests.values()]
    row = {"strategy": s.name, "score": st.mean(vs), "worst": min(vs),
           "growth_mean": st.mean(x["growth"] for x in tests.values()),
           "vs_hold_mean": st.mean(x["vs_hold"] for x in tests.values())}
    for k, x in tests.items():
        row[f"{k}: рост"] = x["growth"]
        row[f"{k}: к доле"] = x["vs_split"]
    return row


def prepare(cfg: Config, paths: int = 30, days: int = 365):
    pools = {k: cfg.pools[k] for k in cfg.pools}
    now = read_pools(pools)
    cs, yields = history.load(pools, days, 5, "binance")
    T, P = [c[0] for c in cs], [c[1] for c in cs]
    q = len(cs) // 4
    quarters = {}
    for i in range(4):
        i0, i1 = i * q, (i + 1) * q + 1 if i < 3 else len(cs)
        quarters[f"{time.strftime('%d.%m.%y', time.gmtime(T[i0]))}–{time.strftime('%d.%m.%y', time.gmtime(T[i1 - 1]))} "
                 f"({P[i1 - 1] / P[i0] - 1:+.0%})"] = (i0, i1)
    pnow = next(iter(now.values()))["sui"]
    last = 60 * 288                                       # реальные последние 60 дней, растянутые до ×2
    lp, lt = P[-last - 1:], T[-last - 1:]
    add = (math.log(2) - math.log(lp[-1] / lp[0])) / last
    replay = ([x - lt[0] for x in lt], [pnow * math.exp(math.log(x / lp[0]) + add * j) for j, x in enumerate(lp)],
              {k: y[-last - 1:] for k, y in yields.items()})
    hist = 180 * 288                                      # сценарии из последних 180 дней
    hc, hy = cs[-hist:], {k: y[-hist:] for k, y in yields.items()}
    scen = {m: list(make_paths(hc, hy, pnow, m, 60, paths, 5, 5, seed=7)) for m in (2.0, 1.0, 0.5)}
    D.update(pcs=pools, spacing={k: v["spacing"] for k, v in now.items()}, costs=cfg.costs, p_now=pnow,
             T=T, P=P, Y=yields, quarters=quarters, replay=replay, scen=scen)


def run(cfg: Config, paths: int = 30, top: int = 15) -> list[dict]:
    t0 = time.time()
    prepare(cfg, paths)
    strategies = grid(list(D["pcs"]), CAPITAL_USD / D["p_now"])
    print(f"данные готовы за {time.time() - t0:.0f} с; стратегий {len(strategies)}, тестов на каждую "
          f"{len(D['quarters']) + 1 + len(D['scen'])} (сценарии по {paths} путей)", flush=True)
    with mp.get_context("fork").Pool() as pool:
        rows = []
        for i, r in enumerate(pool.imap_unordered(evaluate, strategies), 1):
            rows.append(r)
            if i % 20 == 0:
                print(f"  {i}/{len(strategies)} за {time.time() - t0:.0f} с", flush=True)
    rows.sort(key=lambda r: -r["score"])
    _csv(PROC / "sui_optimize.csv", rows)
    print(f"\nЛучшие {top} по среднему результату к «той же доле» (% капитала за тест):")
    for r in rows[:top]:
        print(f"{r['score']:+7.1%} худший {r['worst']:+7.1%} рост ${r['growth_mean']:+7.1%} к холду SUI "
              f"{r['vs_hold_mean']:+7.1%}  {r['strategy']}")
    return rows
