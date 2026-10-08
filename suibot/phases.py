"""Стратегии на всей истории SUI (с 06.2023): два бычьих рывка (×5 и ×9), спады, медвежий год — и хронология фаз.

  python3 bot.py phases            стратегии пула из suibot.toml по большим периодам и кварталам
  python3 bot.py phases --grid     боевая стратегия и её соседи (длина средней, пороги, подтверждение, ширина) —
                                   проверка, что выбор не случайный

Метрика — стоимость к «держать SUI» (то же, что штуки SUI к хранению): на росте — чем ближе к нулю, тем лучше,
на падении — чем выше, тем больше накоплено SUI. Цена — 5-минутные свечи Binance. Доход пула — реальный (счётчики
пула, примерно с 11.2024); раньше пула не было, поэтому две оценки: «равн.» — доход растёт с волатильностью
(коэффициент из реальных данных: комиссии / (σ²/8)), «мед.» — медиана реального дохода. Считает тот же симулятор,
что и бот (suibot.sim), поэтому результат совпадает с тем, что бот сделал бы на этих ценах.
"""
from __future__ import annotations

import bisect
import calendar
import dataclasses
import math
import multiprocessing as mp
import statistics as stt
import time

from sui_pools import daily_checkpoints, pool_history
from suibot import history
from suibot.chain import read_pools
from suibot.config import Config
from suibot.sim import simulate

CAP = 200.0
D = {}          # данные для дочерних процессов (fork)


def gm(y: int, m: int, d: int) -> float:
    return float(calendar.timegm((y, m, d, 0, 0, 0)))


PERIODS = [("3 года", gm(2023, 10, 15), None), ("бычий 1", gm(2023, 10, 15), gm(2024, 3, 28)),
           ("спад 2024", gm(2024, 3, 28), gm(2024, 8, 5)), ("бычий 2", gm(2024, 8, 5), gm(2025, 1, 6)),
           ("с максимума", gm(2025, 1, 6), gm(2025, 10, 6)), ("медвежий год", gm(2025, 10, 6), None)]


def prepare(cfg: Config, pool: str):
    pc = cfg.pools[pool]
    cs = history.binance_candles(gm(2023, 6, 1), time.time(), 5)
    T, P, V = [c[0] for c in cs], [c[1] for c in cs], [c[2] for c in cs]
    cps = daily_checkpoints(int((time.time() - gm(2024, 10, 1)) / 86400))
    pts = None
    for k in range(0, 60, 5):                       # первые дни могут быть раньше создания пула
        try:
            pts = pool_history(pc.object, pc.dex, pc.a_is_sui, cps[k:])[0]
            break
        except Exception:  # noqa: BLE001
            continue
    if not pts:
        raise SystemExit("не удалось прочитать историю пула")
    day_t = [p["t"] for p in pts]
    day_y = [p["y_fee"] + p["y_rew"] for p in pts[:-1]]
    vol, var = {}, {}
    for j, (t, v) in enumerate(zip(T, V)):
        d = int(t // 86400)
        vol[d] = vol.get(d, 0.0) + v
        if j:
            var[d] = var.get(d, 0.0) + math.log(P[j] / P[j - 1]) ** 2
    real, ys = {}, []
    for t, v in zip(T, V):
        i = bisect.bisect_right(day_t, t) - 1
        y = day_y[i] * (v / vol[int(t // 86400)] if vol[int(t // 86400)] else 1 / 288) if 0 <= i < len(day_y) else None
        ys.append(y)
        if y is not None:
            real[int(t // 86400)] = real.get(int(t // 86400), 0.0) + y
    ds = [d for d in real if var.get(d)]
    k_eq = sum(real[d] for d in ds) / sum(var[d] / 8 for d in ds)
    med = stt.median(day_y)
    share = [v / vol[int(t // 86400)] if vol[int(t // 86400)] else 1 / 288 for t, v in zip(T, V)]
    Y = {"равн.": [y if y is not None else k_eq * var.get(int(t // 86400), 0.0) / 8 * s for t, y, s in zip(T, ys, share)],
         "мед.": [y if y is not None else med * s for y, s in zip(ys, share)]}
    D.update(T=T, P=P, Y=Y, pc=pc, sp=read_pools({pool: pc})[pool]["spacing"], costs=cfg.costs, real_from=day_t[0],
             k_eq=k_eq)


def windows():
    T = D["T"]
    named = [(n, bisect.bisect_left(T, a), min(len(T), bisect.bisect_left(T, b) + 1) if b else len(T))
             for n, a, b in PERIODS]
    quarters, a = [], gm(2023, 8, 1)
    while a + 60 * 86400 < T[-1]:
        i0 = bisect.bisect_left(T, a)
        quarters.append((time.strftime("%m.%y", time.gmtime(a)), i0, min(len(T), bisect.bisect_left(T, a + 91 * 86400) + 1)))
        a += 91 * 86400
    return named, quarters


def run(job):
    s, i0, i1, yk, cm, off = job
    T, P = D["T"], D["P"]
    c0 = D["costs"]
    costs = dataclasses.replace(c0, swap_fee=c0.swap_fee * cm, slippage=c0.slippage * cm, gas_sui=c0.gas_sui * cm)
    need = history.trend_days([s]) + 1
    j0 = bisect.bisect_left(T, T[i0] - need * 86400)
    sh = off * 3600                                  # сдвиг времени = другой час «закрытия дня»
    warm = [(T[j] - sh, P[j]) for j in range(j0, i0) if int(T[j]) % 3600 == 0]
    ev = []
    r = simulate(dataclasses.replace(s, capital_sui=CAP / P[i0]), D["pc"], [t - sh for t in T[i0:i1]], P[i0:i1],
                 D["Y"][yk][i0:i1], costs, D["sp"], warm=warm, events=ev)
    return (s.name, *job[1:]), (r["value"] / (CAP / P[i0] * P[i1 - 1]) - 1, [(t + sh, k, p) for t, k, p in ev])


def table(strategies, offsets=(0,)):
    named, quarters = windows()
    P = D["P"]
    jobs = [(s, i0, i1, yk, 1.0, off) for s in strategies for _, i0, i1 in named + quarters for yk in D["Y"]
            for off in offsets]
    jobs += [(s, named[0][1], named[0][2], "равн.", 3.0, off) for s in strategies for off in offsets]
    with mp.get_context("fork").Pool() as pool:
        res = dict(pool.imap_unordered(run, jobs, chunksize=2))
    print("К «держать SUI» (доход пула до " + time.strftime("%m.%Y", time.gmtime(D["real_from"])) +
          f": равн./мед.; изд.×3 — утроенные обмены и газ). Цена SUI: " +
          ", ".join(f"{n} {P[i0]:.2f}→{P[i1 - 1]:.2f}" for n, i0, i1 in named[1:]) + "\n")
    for s in strategies:
        print(f"«{s.name}»" + (f" — час закрытия дня {', '.join(f'{o:02d}:00' for o in offsets)} UTC" if offsets != (0,) else ""))
        for off in offsets:
            g = lambda n, yk="равн.", cm=1.0: res[(s.name, *next((a, b) for x, a, b in named if x == n), yk, cm, off)][0]  # noqa: E731
            q = [(res[(s.name, i0, i1, "равн.", 1.0, off)][0], P[i1 - 1] / P[i0]) for _, i0, i1 in quarters]
            up = [v for v, ch in q if ch >= 1.2]
            other = [v for v, ch in q if ch < 1.2]
            print(f"  3 года {g('3 года'):+.0%}/{g('3 года', 'мед.'):+.0%} (изд.×3 {g('3 года', cm=3.0):+.0%}) | "
                  + " | ".join(f"{n} {g(n):+.0%}" for n, _, _ in named[1:]))
            print(f"  кварталы роста цены ≥ +20% ({len(up)}): в среднем {stt.mean(up):+.0%}, худший {min(up):+.0%}; "
                  f"остальные ({len(other)}): в среднем {stt.mean(other):+.0%}, худший {min(other):+.0%}")
        print()
    return res, named


def timeline(res, s, named):
    """Смены фазы за 3 года у стратегии с фазами: даты, цены и сколько дней держалась фаза."""
    _, i0, i1 = named[0]
    ev = [(t, k, p) for t, k, p in res[(s.name, i0, i1, "равн.", 1.0, 0)][1] if k in ("фаза роста", "конец фазы роста")]
    if not ev:
        return
    print(f"Смены фазы у «{s.name}» (10.2023–сейчас):")
    for j, (t, k, p) in enumerate(ev):
        till = ev[j + 1][0] if j + 1 < len(ev) else time.time()
        p_end = ev[j + 1][2] if j + 1 < len(ev) else D["P"][-1]
        print(f"  {time.strftime('%d.%m.%Y', time.gmtime(t))}  {'📈 рост' if k == 'фаза роста' else '📉 падение/боковик':18s} "
              f"${p:.4f} → ${p_end:.4f} ({p_end / p - 1:+.0%}) за {(till - t) / 86400:.0f} дн.")
    print()


def run_all(cfg: Config, grid: bool = False):
    t0 = time.time()
    live = next((s for s in cfg.strategies if cfg.live and s.name == cfg.live.strategy), None)
    pool = live.pool if live else "cetus_005"
    prepare(cfg, pool)
    print(f"История: {len(D['T']):,} свечей по 5 мин с {time.strftime('%d.%m.%Y', time.gmtime(D['T'][0]))}; "
          f"доход пула {pool} реальный с {time.strftime('%d.%m.%Y', time.gmtime(D['real_from']))} "
          f"(равн.: комиссии = {D['k_eq']:.2f} × σ²/8)\n")
    if grid and live:
        R = dataclasses.replace
        alts = [live]
        if live.phase_ma_days:
            alts += [R(live, name=f"средняя {n} дн.", phase_ma_days=n) for n in (50, 75) if n != live.phase_ma_days]
            alts += [R(live, name=f"пороги ±{h:.1%}", phase_up=h, phase_down=h) for h in (0.025, 0.075)]
            alts += [R(live, name=f"закрытий подряд: {c}", phase_confirm=c) for c in (1, 3) if c != live.phase_confirm]
            alts.append(R(live, name="без фаз", phase_ma_days=None))
        if live.trend_ma_days:
            alts += [R(live, name=f"фильтр тренда {n} дн.", trend_ma_days=n) for n in (30, 75)]
        alts += [R(live, name=f"пул ±{w:.0%}", range_down=w, range_up=w) for w in (0.04, 0.08)
                 if w != live.range_down]
        res, named = table(alts)
        if live.phase_ma_days:
            table([live], offsets=(0, 6, 12, 18))
    else:
        strategies = [s for s in cfg.strategies if s.pool == pool and s.rebalance != "none"]
        res, named = table(strategies)
    if live and live.phase_ma_days:
        timeline(res, live, named)
    print(f"расчёт {time.time() - t0:.0f} с")
