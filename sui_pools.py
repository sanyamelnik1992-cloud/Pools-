#!/usr/bin/env python3
"""Пулы SUI/USDC в сети Sui: реальный доход на единицу ликвидности по истории блокчейна, R и бэктест к холду.

Krystal Sui не поддерживает, а мгновенная «активная ликвидность» на Sui ничего не говорит: боты с узкими
диапазонами и JIT-ликвидность меняют её в сотни раз за минуты. Поэтому доход берётся из счётчиков пула
fee_growth_global и growth_global наград: их прирост — ровно то, что заработала единица ликвидности,
стоявшая в диапазоне при каждом свопе (конкуренция ботов уже учтена, комиссии — за вычетом доли протокола).
Состояние пула на каждый день — Sui GraphQL (object atCheckpoint); цена SUI — из самого пула (USDC = $1);
цены токенов наград — DefiLlama coins; объём для «витринного» APR на TVL — GeckoTerminal.

fr — доход единицы ликвидности за год в % от стоимости полнодиапазонной позиции; R = fr ÷ σ²/8
(σ²/8 — ожидаемые потери полного диапазона от движения цены, LVR). R > 1 — комиссии их перекрывают.
Позиция ±w получает fr × CF(w), пока цена в диапазоне: CF = 1 / (1 − 1/√(1+w)), для ±20% это 11.5.
Концентрация рынка CE = витринный APR комиссий на TVL ÷ fr: во сколько раз средняя ликвидность пула
уже полного диапазона. Если CE больше CF вашей позиции, вы зарабатываете меньше витринного APR.
Бэктест: позиция ±w на $10k открывается в начале каждого из трёх 30-дневных окон; комиссии и награды
начисляются по фактическому дневному доходу единицы ликвидности, пока цена в диапазоне.

Режим --narrow: узкие диапазоны на часовых ценах (GeckoTerminal) для самого глубокого пула — позиция из SUI
(половина меняется на USDC) с пересборкой вокруг текущей цены при выходе из диапазона и без неё, против
холда SUI и холда 50/50; каждое 30-дневное окно начинается заново.

С --range LOW HIGH: свой диапазон (USDC за SUI) относительно текущей цены на скользящих 30-дневных окнах —
без пересборки, с пересборкой сразу и через сутки вне диапазона — против холда SUI и против «продать ту же долю
SUI и держать» (это и показывает, что добавляет сам пул).

Запуск:  python3 sui_pools.py [--days 90]
         python3 sui_pools.py --narrow [--days 150] [--usd 4000] [--range 1.20 1.30]
Результат: data/processed/sui_pools.json, data/processed/sui_narrow.json, data/processed/sui_range.json
"""
from __future__ import annotations

import argparse
import bisect
import math
import statistics as st
import time
from datetime import datetime

import requests

from lpscan.common import PROC, get_json, save
from lpscan.metrics import amounts_for_L, value_per_L

GQL = "https://graphql.mainnet.sui.io/graphql"   # история объектов; публичный JSON-RPC Mysten отключён
GECKO = "https://api.geckoterminal.com/api/v2/networks/sui-network"
SUI = "0x2::sui::SUI"
USDC = "0xdba34672e30cb065b1f93e3ab55318768fd6fef66c15942c9f7cb846e2f900e7::usdc::USDC"
Q64, U128 = 2 ** 64, 2 ** 128
# поля объекта пула у разных DEX: sqrt-цена, рост комиссий монет a/b, список наград, тип и рост награды,
# комиссия и доля протокола (в миллионных), резервы a/b
LAYOUT = {
    "bluefin": ("current_sqrt_price", "fee_growth_global_coin_a", "fee_growth_global_coin_b", "reward_infos",
                "reward_coin_type", "reward_growth_global", "fee_rate", "protocol_fee_share", "coin_a", "coin_b"),
    "cetus": ("current_sqrt_price", "fee_growth_global_a", "fee_growth_global_b", "rewarder_manager.rewarders",
              "reward_coin", "growth_global", "fee_rate", None, "coin_a", "coin_b"),
    "turbos": ("sqrt_price", "fee_growth_global_a", "fee_growth_global_b", "reward_infos",
               "vault_coin_type", "growth_global", "fee", "fee_protocol", "coin_a", "coin_b"),
    "momentum": ("sqrt_price", "fee_growth_global_x", "fee_growth_global_y", "reward_infos",
                 "reward_coin_type", "reward_growth_global", "swap_fee_rate", "protocol_fee_share",
                 "reserve_x", "reserve_y"),
}
CETUS_PROTOCOL_SHARE = 0.20   # у Cetus доля протокола лежит в глобальном конфиге, а не в пуле
# название: (объект пула, DEX, монета a — SUI; иначе a = USDC, b = SUI)
POOLS = {
    "Bluefin SUI/USDC 0.175%": ("0x15dbcac854b1fc68fc9467dbd9ab34270447aabd8cc0e04a5864d95ccb86b74a", "bluefin", True),
    "Cetus USDC/SUI 0.05%": ("0x51e883ba7c0b566a26cbc8a94cd33eb0abd418a77cc1e60ad22fd9b1f29cd2ab", "cetus", False),
    "Cetus USDC/SUI 0.25%": ("0xb8d7d9e66a60c239e7a60110efcf8de6c705580ed924d0dde141f4a0e2c90105", "cetus", False),
    "Turbos SUI/USDC 0.05%": ("0x0df4f02d0e210169cb6d5aabd03c3058328c06f2c4dbb0804faa041159c78443", "turbos", True),
    "Momentum SUI/USDC 0.175%": ("0x455cf8d2ac91e7cb883f515874af750ed3cd18195c970b7a2d46235ac2b0c388", "momentum", True),
}
WIDTHS = (0.1, 0.2, 0.3, 0.5, None)   # None — полный диапазон
NARROW_POOL = "Cetus USDC/SUI 0.25%"   # самый глубокий пул SUI/USDC
NARROW_WIDTHS = (0.02, 0.05, 0.1, 0.2, 0.3)
SWAP_COST = 0.001   # комиссия лучшего маршрута и проскальзывание на обмениваемую сумму при пересборке
GAS_USD = 0.02      # транзакция в Sui стоит доли цента — берём с запасом


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def gql(query: str) -> dict:
    err = None
    for a in range(6):
        try:
            j = requests.post(GQL, json={"query": query}, timeout=90).json()
            if j.get("data") and not j.get("errors"):
                return j["data"]
            err = j.get("errors")
        except (requests.RequestException, ValueError) as e:
            err = str(e)
        time.sleep(2 ** a)
    raise RuntimeError(f"Sui GraphQL: {err}")


def ts(s: str) -> float:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def checkpoint_times(seqs: list[int]) -> list[float]:
    out = []
    for i in range(0, len(seqs), 40):
        part = seqs[i:i + 40]
        d = gql("{" + " ".join(f"c{j}: checkpoint(sequenceNumber:{n}){{ timestamp }}" for j, n in enumerate(part)) + "}")
        out += [ts(d[f"c{j}"]["timestamp"]) for j in range(len(part))]
    return out


def daily_checkpoints(days: int) -> list[tuple[int, float]]:
    """Чекпоинты с шагом в сутки от текущего назад: (номер, unix-время); два прохода интерполяции."""
    now = gql("{ checkpoint { sequenceNumber timestamp } }")["checkpoint"]
    n1, t1 = int(now["sequenceNumber"]), ts(now["timestamp"])
    probe = n1 - days * 380_000
    rate = (n1 - probe) / (t1 - checkpoint_times([probe])[0])
    targets = [t1 - d * 86400 for d in range(days, -1, -1)]
    est = [min(n1, round(n1 - (t1 - t) * rate)) for t in targets]
    got = checkpoint_times(est)
    est = [min(n1, round(n + (t - g) * rate)) for n, t, g in zip(est, targets, got)]
    return list(zip(est, checkpoint_times(est)))


def norm_type(t: str) -> str:
    t = t if t.startswith("0x") else "0x" + t
    addr, rest = t.split("::", 1)
    return SUI if int(addr, 16) == 2 and rest == "sui::SUI" else t


def pool_states(oid: str, seqs: list[int]) -> list[dict | None]:
    out = []
    for i in range(0, len(seqs), 15):
        part = seqs[i:i + 15]
        d = gql("{" + " ".join(f'c{j}: object(address:"{oid}", atCheckpoint:{n}){{ asMoveObject {{ contents {{ json }} }} }}'
                               for j, n in enumerate(part)) + "}")
        out += [(d[f"c{j}"] or {}).get("asMoveObject", {}).get("contents", {}).get("json") for j in range(len(part))]
    return out


def parse(js: dict, dex: str, a_is_sui: bool) -> dict:
    sqk, fak, fbk, rlk, rck, rgk, feek, protk, rak, rbk = LAYOUT[dex]
    rews = js
    for k in rlk.split("."):
        rews = rews[k]
    sq = int(js[sqk]) / Q64                     # √(монет b в мин. единицах за мин. единицу a)
    sui = sq * sq * 1e3 if a_is_sui else 1e3 / (sq * sq)   # SUI 9 знаков, USDC 6 знаков, USDC = $1
    ua, ub = (sui / 1e9, 1e-6) if a_is_sui else (1e-6, sui / 1e9)   # $ за мин. единицу монет a и b
    return {"sq": sq, "sui": sui, "ua": ua, "ub": ub, "fa": int(js[fak]), "fb": int(js[fbk]),
            "rew": {norm_type(r[rck]): int(r[rgk]) for r in rews}, "rewards_raw": rews,
            "fee": int(js[feek]) / 1e6, "prot": int(js[protk]) / 1e6 if protk else CETUS_PROTOCOL_SHARE,
            "tvl": int(js[rak]) * ua + int(js[rbk]) * ub}


def reward_prices(types: set[str], t0: float) -> dict:
    """Дневные цены и знаки токенов наград (DefiLlama); USDC = $1, SUI берётся из пула."""
    out = {}
    for t in types - {SUI}:
        if t == USDC:
            out[t] = {"dec": 6, "prices": [(0, 1.0)]}
            continue
        try:
            c = get_json(f"https://coins.llama.fi/chart/sui:{t}", cache_ttl=3600,
                         params={"start": int(t0) - 86400, "span": int((time.time() - t0) / 86400) + 3,
                                 "period": "1d"})["coins"][f"sui:{t}"]
            out[t] = {"dec": c.get("decimals", 9), "prices": [(p["timestamp"], p["price"]) for p in c["prices"]]}
        except (RuntimeError, KeyError):
            out[t] = None                       # нет цены — награда не учитывается
    return out


def price_at(pr: dict | None, t: float) -> float | None:
    if not pr:
        return None
    return min(pr["prices"], key=lambda p: abs(p[0] - t))[1]


def cf(w: float | None) -> float:
    """Во сколько раз позиция ±w концентрированнее полного диапазона (в центре диапазона)."""
    return 1.0 if w is None else 1 / (1 - 1 / math.sqrt(1 + w))


def backtest(days: list[dict], w: float | None, with_rewards: bool, usd: float = 10_000) -> dict:
    """days — точки окна: цена SUI, доля дохода единицы ликвидности за следующий день (от полного диапазона)."""
    P0 = days[0]["sui"]
    pa, pb = (P0 / (1 + w), P0 * (1 + w)) if w else (0.0, math.inf)

    def amounts(L, P):                         # x — SUI, y — USDC при цене P (USDC за SUI)
        Pc = min(max(P, pa), pb)
        x = L * (1 / math.sqrt(Pc) - (1 / math.sqrt(pb) if pb < math.inf else 0))
        return x, L * (math.sqrt(Pc) - math.sqrt(pa))

    x0, y0 = amounts(1.0, P0)
    L = usd / (x0 * P0 + y0)
    x0, y0 = x0 * L, y0 * L
    inc = inr = 0.0
    for d, nxt in zip(days, days[1:]):
        k = ((pa <= d["sui"] <= pb) + (pa <= nxt["sui"] <= pb)) / 2   # доля дня в диапазоне (грубо, по концам)
        y = d["y_fee"] + (d["y_rew"] if with_rewards else 0)
        inc += k * y * 2 * L * math.sqrt(math.sqrt(d["sui"] * nxt["sui"]))
        inr += k
    P1 = days[-1]["sui"]
    x1, y1 = amounts(L, P1)
    n = len(days) - 1
    lp = x1 * P1 + y1 + inc
    ann = 365 / n * 100 / usd
    return {"in_range_pct": inr / n * 100, "income_apr": inc * ann, "vs_hold_apr": (lp - x0 * P1 - y0) * ann,
            "vs_sui_apr": (lp - usd * P1 / P0) * ann, "vs_usdc_apr": (lp - usd) * ann}


def gecko_volume(oid: str) -> float | None:
    for a in range(5):
        try:
            r = requests.get(f"{GECKO}/pools/{oid}/ohlcv/day", params={"limit": 30, "currency": "usd"}, timeout=30)
            if r.status_code == 200:
                time.sleep(2.5)
                v = [x[5] for x in r.json()["data"]["attributes"]["ohlcv_list"]]
                return sum(v) / len(v)
        except (requests.RequestException, ValueError, KeyError):
            pass
        time.sleep(10 * (a + 1))
    return None


def current_reward_apr(p: dict, prices: dict) -> tuple[float, list]:
    """Текущие эмиссии наград на TVL и дата окончания (если программа её хранит)."""
    now, total, info = time.time(), 0.0, []
    for r in p["rewards_raw"]:
        t = norm_type(r.get("reward_coin_type") or r.get("reward_coin") or r.get("vault_coin_type"))
        rate = int(r.get("reward_per_seconds") or r.get("emissions_per_second") or 0) / Q64
        end = int(r.get("ended_at_seconds") or 0)
        if not rate or (end and end < now):
            continue
        px, dec = (p["sui"], 9) if t == SUI else (price_at(prices.get(t), now), (prices.get(t) or {}).get("dec", 9))
        if px is None:
            continue
        apr = rate / 10 ** dec * px * 365 * 86400 / p["tvl"] * 100
        total += apr
        info.append({"token": t.split("::")[-1], "apr_on_tvl": apr,
                     "ends": datetime.utcfromtimestamp(end).strftime("%Y-%m-%d") if end else None})
    return total, info


def pool_history(oid: str, dex: str, a_is_sui: bool, cps: list[tuple[int, float]]) -> tuple[list[dict], dict]:
    """Состояние пула на каждый чекпоинт и доход единицы ликвидности за следующие сутки: y_fee и y_rew —
    доля стоимости полнодиапазонной позиции (комиссии уже без доли протокола, награды по ценам дня)."""
    raw = pool_states(oid, [n for n, _ in cps])
    pts = [dict(parse(js, dex, a_is_sui), t=t) for js, (_, t) in zip(raw, cps) if js]
    prices = reward_prices({k for p in pts for k in p["rew"]}, pts[0]["t"])
    for p, q in zip(pts, pts[1:]):
        fee = ((q["fa"] - p["fa"]) % U128 * q["ua"] + (q["fb"] - p["fb"]) % U128 * q["ub"]) / Q64
        rew = 0.0
        for t, g in q["rew"].items():
            dg = (g - p["rew"].get(t, g)) % U128
            if not dg:
                continue
            px, dec = (q["sui"], 9) if t == SUI else (price_at(prices.get(t), q["t"]), (prices.get(t) or {}).get("dec", 9))
            rew += dg / Q64 * (px or 0) / 10 ** dec
        v_full = 1 / q["sq"] * q["ua"] + q["sq"] * q["ub"]   # $ полного диапазона на единицу ликвидности
        p["y_fee"], p["y_rew"] = fee / v_full, rew / v_full
    return pts, prices


def gecko_candles(oid: str, a_is_sui: bool, since: float, minutes: int = 60) -> list[tuple[float, float, float]]:
    """Свечи пула (GeckoTerminal) с since: начало свечи, цена SUI на закрытии, объём; шаг minutes (5, 15, 60…).
    Свечи без сделок дописываются с прежней ценой и нулевым объёмом."""
    frame, agg = ("minute", minutes) if minutes < 60 else ("hour", minutes // 60)
    rows, before = {}, None
    while True:
        params = {"aggregate": agg, "limit": 1000, "currency": "usd", "token": "base" if a_is_sui else "quote"}
        if before:
            params["before_timestamp"] = before
        lst = []
        for a in range(6):
            try:
                r = requests.get(f"{GECKO}/pools/{oid}/ohlcv/{frame}", params=params, timeout=60)
                if r.status_code == 200:
                    lst = r.json()["data"]["attributes"]["ohlcv_list"]
                    break
            except (requests.RequestException, ValueError, KeyError):
                pass
            time.sleep(10 * (a + 1))
        time.sleep(2.5)
        rows.update({x[0]: (x[4], x[5]) for x in lst})
        if not lst or min(x[0] for x in lst) <= since:
            break
        before = min(x[0] for x in lst)
    ts_ = sorted(t for t in rows if t >= since - minutes * 60)
    out, last = [], rows[ts_[0]][0]
    for t in range(ts_[0], ts_[-1] + 1, minutes * 60):
        c, v = rows.get(t, (last, 0.0))
        out.append((t, c, v))
        last = c
    return out


def backtest_hourly(hours: list[tuple], days: list[dict], lo: float, hi: float, rebalance: bool, usd: float,
                    wait_h: int = 1) -> dict:
    """Позиция с границами P·lo…P·hi от цены открытия P, собранная из SUI на usd (нужная доля меняется на USDC),
    на часовых ценах. Доход — фактический дневной доход единицы ликвидности, разнесённый по часам пропорционально
    объёму, пока цена в диапазоне. rebalance — когда цена закрытия пробыла вне диапазона wait_h часов подряд,
    позиция пересобирается вокруг текущей цены с теми же относительными границами (своп с издержками)."""
    day_t = [d["t"] for d in days]
    idx = [bisect.bisect_right(day_t, t) - 1 for t, _, _ in hours]
    vol_day: dict[int, float] = {}
    for i, (_, _, v) in zip(idx, hours):
        vol_day[i] = vol_day.get(i, 0.0) + v

    def sui_share(P):                           # доля SUI по стоимости в новой позиции
        x, y = amounts_for_L(1.0, P, P * lo, P * hi)
        return x * P / (x * P + y)

    def open_at(V, P):
        return V / value_per_L(P, P * lo, P * hi), P * lo, P * hi

    P0 = prev = hours[0][1]
    f0 = sui_share(P0)
    cost = usd * (1 - f0) * SWAP_COST + GAS_USD
    L, pa, pb = open_at(usd - cost, P0)
    inc = inr = 0.0
    n_reb = out_h = 0
    for (_, P, v), i in zip(hours[1:], idx[1:]):
        if 0 <= i < len(days) - 1:
            d = days[i]
            share = v / vol_day[i] if vol_day[i] else 1 / 24
            k = ((pa <= prev <= pb) + (pa <= P <= pb)) / 2
            inc += k * (d["y_fee"] + d["y_rew"]) * share * 2 * L * math.sqrt(math.sqrt(prev * P))
            inr += k
        out_h = 0 if pa <= P <= pb else out_h + 1
        if rebalance and out_h >= wait_h:
            x, y = amounts_for_L(L, P, pa, pb)
            V = x * P + y
            c = abs(x * P - V * sui_share(P)) * SWAP_COST + GAS_USD
            cost += c
            L, pa, pb = open_at(V - c, P)
            n_reb += 1
            out_h = 0
        prev = P
    x, y = amounts_for_L(L, prev, pa, pb)
    end = x * prev + y + inc
    return {"income": inc, "costs": cost, "rebalances": n_reb, "in_range_pct": inr / (len(hours) - 1) * 100,
            "end_value": end, "sui_share_end": x * prev / (x * prev + y) * 100, "sui_change": prev / P0 - 1,
            "vs_sui": end - usd * prev / P0, "vs_split": end - usd * (f0 * prev / P0 + 1 - f0)}


def narrow(days: int, usd: float):
    """Узкие диапазоны с пересборкой и без неё на часовых ценах; окна по 30 дней и весь период целиком."""
    oid, dex, a_is_sui = POOLS[NARROW_POOL]
    cps = daily_checkpoints(days)
    pts, _ = pool_history(oid, dex, a_is_sui, cps)
    hours = [h for h in gecko_candles(oid, a_is_sui, pts[0]["t"]) if pts[0]["t"] <= h[0] <= pts[-1]["t"]]
    log(f"{NARROW_POOL}: {len(pts)} дней истории пула, {len(hours)} часовых свечей")
    spans = [(pts[i * 30]["t"], pts[(i + 1) * 30]["t"]) for i in range((len(pts) - 1) // 30)] + [(pts[0]["t"], pts[-1]["t"])]
    out = {"generated_at": int(time.time()), "pool": NARROW_POOL, "usd": usd, "swap_cost": SWAP_COST, "periods": []}
    for a, b in spans:
        hs = [h for h in hours if a <= h[0] <= b]
        per = {"start": a, "end": b, "sui_change": hs[-1][1] / hs[0][1] - 1, "results": {}}
        for w in NARROW_WIDTHS + (None,):
            for reb in ((False, True) if w else (False,)):
                lo, hi = (1 / (1 + w), 1 + w) if w else (1e-6, 1e6)
                per["results"][f"{'±%g%%' % (w * 100) if w else 'полный'}{' пересборка' if reb else ''}"] = \
                    backtest_hourly(hs, pts, lo, hi, reb, usd)
        out["periods"].append(per)
        log(f"{datetime.utcfromtimestamp(a):%d.%m}–{datetime.utcfromtimestamp(b):%d.%m} SUI {per['sui_change']:+.0%}: " +
            " | ".join(f"{k} {r['vs_sui']:+.0f}$ (доход {r['income']:.0f}$, пересборок {r['rebalances']})"
                       for k, r in per["results"].items() if k.endswith("пересборка") or k == "полный"))
    save(PROC / "sui_narrow.json", out)


def range_rolling(days: int, usd: float, low: float, high: float, step_days: int = 3):
    """Диапазон low–high (USDC за SUI) относительно текущей цены на скользящих 30-дневных окнах с шагом step_days."""
    oid, dex, a_is_sui = POOLS[NARROW_POOL]
    pts, _ = pool_history(oid, dex, a_is_sui, daily_checkpoints(days))
    hours = [h for h in gecko_candles(oid, a_is_sui, pts[0]["t"]) if pts[0]["t"] <= h[0] <= pts[-1]["t"]]
    P = pts[-1]["sui"]
    lo, hi = low / P, high / P
    policies = {"без пересборки": (False, 1), "пересборка сразу": (True, 1), "пересборка через сутки": (True, 24)}
    H = 30 * 24
    starts = range(0, len(hours) - H, step_days * 24)
    out = {"generated_at": int(time.time()), "pool": NARROW_POOL, "price": P, "range": [low, high], "usd": usd,
           "windows": len(starts), "policies": {}}
    log(f"SUI ${P:.4f}, диапазон {low}–{high} ({lo - 1:+.1%}/{hi - 1:+.1%}), позиция ${usd:,.0f}, "
        f"{len(starts)} окон по 30 дней за {days} дней")
    for name, (reb, wait) in policies.items():
        rs = [backtest_hourly(hours[s:s + H + 1], pts, lo, hi, reb, usd, wait) for s in starts]
        whole = backtest_hourly(hours, pts, lo, hi, reb, usd, wait)

        def stats(key, rows):
            v = sorted(r[key] for r in rows)
            return {"median": st.median(v), "mean": st.mean(v), "p10": v[len(v) // 10], "p90": v[len(v) * 9 // 10],
                    "min": v[0], "max": v[-1], "share_positive": sum(x > 0 for x in v) / len(v)} if v else None

        by_dir = {lbl: [r for r in rs if f(r["sui_change"])] for lbl, f in
                  (("рост >5%", lambda c: c > 0.05), ("боковик", lambda c: -0.05 <= c <= 0.05),
                   ("падение >5%", lambda c: c < -0.05))}
        out["policies"][name] = {
            "vs_sui": stats("vs_sui", rs), "vs_split": stats("vs_split", rs),
            "income_median": st.median(r["income"] for r in rs), "rebalances_median": st.median(r["rebalances"] for r in rs),
            "in_range_median": st.median(r["in_range_pct"] for r in rs),
            "by_direction": {k: {"windows": len(g), "vs_sui": stats("vs_sui", g), "vs_split": stats("vs_split", g)}
                             for k, g in by_dir.items()},
            "whole_period": whole}
        a, b = out["policies"][name]["vs_sui"], out["policies"][name]["vs_split"]
        log(f"{name:23s} к холду SUI: медиана {a['median']:+5.0f}$, среднее {a['mean']:+5.0f}$, лучше в {a['share_positive']:.0%} | "
            f"к «продать долю»: медиана {b['median']:+5.0f}$, среднее {b['mean']:+5.0f}$, худшее {b['min']:+5.0f}$, лучше в "
            f"{b['share_positive']:.0%} | доход за месяц {out['policies'][name]['income_median']:.0f}$ | весь период: "
            f"{whole['vs_sui']:+.0f}$ к холду SUI, {whole['vs_split']:+.0f}$ к «продать долю»")
    save(PROC / "sui_range.json", out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=None, help="глубина истории, кратна 30 (90, для --narrow 150)")
    ap.add_argument("--narrow", action="store_true", help="узкие диапазоны с пересборкой на часовых ценах")
    ap.add_argument("--usd", type=float, default=4000, help="размер позиции для --narrow")
    ap.add_argument("--range", type=float, nargs=2, metavar=("LOW", "HIGH"),
                    help="с --narrow: свой диапазон в USDC за SUI, скользящие 30-дневные окна")
    a = ap.parse_args()
    days = (a.days or (150 if a.narrow else 90)) // 30 * 30
    if a.narrow and a.range:
        return range_rolling(days, a.usd, *a.range)
    if a.narrow:
        return narrow(days, a.usd)
    cps = daily_checkpoints(days)
    log(f"чекпоинты: {len(cps)} с {datetime.utcfromtimestamp(cps[0][1]):%Y-%m-%d %H:%M} UTC")
    out = {"generated_at": int(time.time()), "days": days, "pools": {}}
    for name, (oid, dex, a_is_sui) in POOLS.items():
        pts, prices = pool_history(oid, dex, a_is_sui, cps)
        rets = [math.log(q["sui"] / p["sui"]) for p, q in zip(pts, pts[1:])]
        sigma = st.pstdev(rets) * math.sqrt(365)
        lvr = sigma ** 2 / 8 * 100
        dt = (pts[-1]["t"] - pts[0]["t"]) / 86400

        def ann(key, last):                     # % годовых за последние last дней
            seg = pts[-last - 1:-1]
            return sum(p[key] for p in seg) * 365 / last * 100

        fr30, fr_all, rr30, rr_all = ann("y_fee", 30), ann("y_fee", len(pts) - 1), ann("y_rew", 30), ann("y_rew", len(pts) - 1)
        cur = pts[-1]
        vol = gecko_volume(oid)
        fee_tvl = vol * cur["fee"] * (1 - cur["prot"]) * 365 / cur["tvl"] * 100 if vol else None
        rew_tvl, rew_info = current_reward_apr(cur, prices)
        windows = [pts[i * 30:(i + 1) * 30 + 1] for i in range(len(pts) // 30)]
        bt = {lbl: {str(w): [backtest(win, w, rw) for win in windows] for w in WIDTHS}
              for lbl, rw in (("fees", False), ("fees_rewards", True))}
        out["pools"][name] = {
            "object": oid, "dex": dex, "sui_price": cur["sui"], "tvl": cur["tvl"], "sigma": sigma, "lvr_full_pct": lvr,
            "history_days": dt, "vol_day_30d": vol, "fee_apr_on_tvl": fee_tvl, "reward_apr_on_tvl": rew_tvl,
            "rewards": rew_info, "fee_per_liquidity_30d": fr30, "fee_per_liquidity_all": fr_all,
            "reward_per_liquidity_30d": rr30, "reward_per_liquidity_all": rr_all,
            "R_30d": fr30 / lvr, "R_all": fr_all / lvr, "R_rewards_all": (fr_all + rr_all) / lvr,
            "market_concentration": fee_tvl / fr30 if fee_tvl and fr30 else None,
            "position_apr_30d": {str(w): {"fees": fr30 * cf(w), "rewards": rr30 * cf(w)} for w in WIDTHS},
            "windows_sui_change": [w[-1]["sui"] / w[0]["sui"] - 1 for w in windows], "backtest": bt,
            "daily": [{k: p.get(k) for k in ("t", "sui", "y_fee", "y_rew", "tvl")} for p in pts],
        }
        r = out["pools"][name]
        b = bt["fees_rewards"]
        log(f"{name:25s} TVL ${cur['tvl'] / 1e6:5.2f}M | витрина: комиссии {fee_tvl or 0:5.1f}% + награды {rew_tvl:5.1f}% на TVL | "
            f"на ед. ликв.: комиссии {fr30:5.2f}% (за {dt:.0f} дн. {fr_all:5.2f}%), награды {rr30:5.2f}% | "
            f"концентрация ×{r['market_concentration'] or 0:,.0f} | R={r['R_all']:.2f} (с наградами {r['R_rewards_all']:.2f}) | "
            f"±20% к холду: {'/'.join('%+.0f' % x['vs_hold_apr'] for x in b['0.2'])}, ±50%: "
            f"{'/'.join('%+.0f' % x['vs_hold_apr'] for x in b['0.5'])}")
    first = next(iter(out["pools"].values()))
    out.update(sigma=first["sigma"], lvr_full_pct=first["lvr_full_pct"], windows_sui_change=first["windows_sui_change"])
    log(f"SUI ${first['sui_price']:.3f}, σ {first['sigma']:.0%} → σ²/8 = {first['lvr_full_pct']:.1f}% в год; "
        f"окна по 30 дней, изменение SUI: {', '.join('%+.0f%%' % (x * 100) for x in first['windows_sui_change'])}")
    save(PROC / "sui_pools.json", out)


if __name__ == "__main__":
    main()
