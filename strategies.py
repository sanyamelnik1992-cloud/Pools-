#!/usr/bin/env python3
"""Пулы из стейблов, ETH и BTC: какие устойчиво перекрывают холд, плюс продвинутые стратегии.

1. Бэктест всех CL-пулов, где оба токена — стейбл/ETH/BTC (TVL ≥ $300k, история ≥ 85 дней),
   на трёх независимых 30-дневных окнах: позиция открывается заново в начале каждого окна.
   «Устойчиво перекрывает холд» = результат к холду > 0 во всех трёх окнах.
2. Дельта-нейтральная LP: позиция X/USDC + шорт перпа X на Hyperliquid (реальный funding по часам).
3. Базис-трейд (спот/LST + шорт перпа) и ориентиры: lending стейблов, стейкинг ETH, ставки займа.

Запуск:  python3 strategies.py   (после collect.py и analyze.py)
Результат: data/processed/strategies.json, data/processed/hold_beaters.csv
"""
from __future__ import annotations

import statistics as st
import time
from collections import defaultdict

import requests

from analyze import _csv, build_quote_series
from collect import active_liquidity
from lpscan.clean import clean_pool
from lpscan.common import (CHAINS, KRYSTAL_BASE, LLAMA_YIELDS, PROC, RAW, get_json, krystal_headers,
                           load, save)
from lpscan.metrics import (backtest_cl, backtest_hedged, history_metrics, human_L, slice_hist)

OK_CLASSES = {"stable", "eth", "btc"}
WIDTHS = {
    "Стейбл/стейбл": [0.0005, 0.001, 0.0025, 0.005, 0.01],
    "Коррелир. (ETH/LST, BTC/BTC)": [0.001, 0.0025, 0.005, 0.01, 0.02],
    "ETH/BTC": [0.025, 0.05, 0.10, 0.15, 0.20, 0.30],
    "Голубая фишка/стейбл": [0.05, 0.10, 0.15, 0.20, 0.30, 0.50],
}
BAD_FLAGS = {"цена альта не подтверждена", "мёртвый пул: объёма нет", "нет цены у альта"}
HL = "https://api.hyperliquid.xyz/info"


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def hl_funding(coin: str, days: int = 92) -> dict[int, float]:
    """Почасовой funding Hyperliquid: {час: ставка}. Кэш в data/raw/hl_funding_<coin>.json."""
    path = RAW / f"hl_funding_{coin}.json"
    now = int(time.time() * 1000)
    if path.exists() and time.time() - path.stat().st_mtime < 6 * 3600:
        rows = load(path)
    else:
        rows, start = [], now - days * 86400 * 1000
        while True:
            r = requests.post(HL, json={"type": "fundingHistory", "coin": coin, "startTime": start},
                              timeout=30).json()
            if not r:
                break
            rows += r
            if len(r) < 500 or r[-1]["time"] >= now - 3600 * 1000:
                break
            start = r[-1]["time"] + 1
        save(path, rows, compact=True)
    return {int(x["time"]) // 1000 // 3600 * 3600: float(x["fundingRate"]) for x in rows}


def benchmarks() -> dict:
    """Простые альтернативы: lending стейблов, стейкинг ETH, ставки займа (DefiLlama)."""
    pools = get_json(f"{LLAMA_YIELDS}/pools")["data"]
    lb = {x["pool"]: x for x in get_json(f"{LLAMA_YIELDS}/lendBorrow")}

    def pick(project, chain, symbol, meta=None):
        c = [p for p in pools if p["project"] == project and p["chain"] == chain and p["symbol"] == symbol
             and (meta is None or p.get("poolMeta") == meta)]
        if not c:
            return None
        p = max(c, key=lambda p: p["tvlUsd"])
        b = lb.get(p["pool"], {})
        return {"project": project, "chain": chain, "symbol": symbol, "tvl": p["tvlUsd"],
                "apy": p.get("apy"), "apy_mean30": p.get("apyMean30d"), "borrow": b.get("apyBaseBorrow")}

    out = [pick("aave-v3", "Arbitrum", "USDC"), pick("aave-v3", "Base", "USDC"),
           pick("fluid-lending", "Arbitrum", "USDC"), pick("fluid-lending", "Base", "USDC"),
           pick("sky-lending", "Arbitrum", "SUSDS"), pick("lido", "Ethereum", "STETH"),
           pick("aave-v3", "Arbitrum", "WETH"), pick("aave-v3", "Base", "WETH"),
           pick("aave-v3", "Ethereum", "USDC", None)]
    return [x for x in out if x]


def windows(hist_end: int, n: int = 3, days: int = 30):
    return [(hist_end - (n - i) * days * 86400, hist_end - (n - i - 1) * days * 86400) for i in range(n)]


def main():
    H = krystal_headers()
    rd = load(PROC / "report_data.json")
    by_key = {p["key"]: p for p in rd["pools"]}
    results, hedged, universe_out = [], [], []
    staked = load(RAW / "rpc_checks.json").get("aero_staked", {})
    funding = {"eth": hl_funding("ETH"), "btc": hl_funding("BTC")}

    for cid in CHAINS:
        raw = load(RAW / f"krystal_pools_{cid}.json")
        hist = load(RAW / f"krystal_history_{cid}.json")
        ticks = load(RAW / f"krystal_ticks_{cid}.json")
        recs = [clean_pool(p) for p in raw]
        for r in recs:
            if r["key"] in hist:
                r.update(history_metrics(hist[r["key"]]))
        qser = build_quote_series(recs, hist)
        uni = [r for r in recs if r["class0"] in OK_CLASSES and r["class1"] in OK_CLASSES and r["is_cl"]
               and r["tvl"] >= 3e5 and r.get("hist_days", 0) >= 85 and not (set(r["flags"]) & BAD_FLAGS)]
        log(f"{CHAINS[cid]['name']}: пулов в выборке {len(uni)}")
        for r in uni:
            t = ticks.get(r["key"])
            if not t:
                try:
                    tk = get_json(f"{KRYSTAL_BASE}/v1/pools/{cid}/{r['address']}/ticks", headers=H,
                                  params={"factoryAddress": r["factory"]})
                    if isinstance(tk, list) and tk:
                        tick, L = active_liquidity(tk, r["sqrt_price_x96"])
                        t = {"tick": tick, "active_L": str(L)}
                except RuntimeError as e:
                    log("  ! ticks", r["pair"], e)
            if not t or t.get("active_L") in (None, "None", "0"):
                continue
            L_h = human_L(int(t["active_L"]), r["dec0"], r["dec1"])
            order = {"stable": 0, "eth": 1, "btc": 2}
            quote_is_0 = order[r["class0"]] < order[r["class1"]]
            qcls = r["class0"] if quote_is_0 else r["class1"]
            quote = None if qcls == "stable" else qser.get(qcls)
            if qcls != "stable" and quote is None:
                continue
            h = hist[r["key"]]
            wins = windows(h[-1]["timestamp"])
            share = (staked.get(r["key"]) or {}).get("share") or 1.0
            modes = [("Пассивно", False, True, 0.0), ("Авто-ребаланс", True, True, 0.0)]
            if r["reward_usd_day"] > 0 and r["dex"] == "Aerodrome Slipstream":
                modes += [("Стейк AERO, пассивно", False, False, r["reward_usd_day"]),
                          ("Стейк AERO, авто-ребаланс", True, False, r["reward_usd_day"])]
            elif r["reward_usd_day"] > 0:
                modes += [("Комиссии + фарм наград, пассивно", False, True, r["reward_usd_day"])]
            universe_out.append({k: r.get(k) for k in ("key", "chain", "dex", "pair", "fee_pct", "category",
                                                        "tvl", "fee_apr_30d", "reward_apr", "sigma")})
            for label, reb, earn, rew in modes:
                for w in WIDTHS[r["category"]] + [None]:
                    if w is None and reb:
                        continue
                    per = []
                    for a, b in wins:
                        res = backtest_cl(slice_hist(h, a, b), L_h, r["tvl"], w, r["fee_pct"],
                                          invert=quote_is_0, quote_usd=quote, rebalance=reb,
                                          reward_usd_day=rew, earn_fees=earn,
                                          staked_share=min(1.0, max(0.05, share)))
                        if res:
                            per.append(res)
                    if len(per) < 3:
                        continue
                    nets = [x["net_vs_hodl_apr"] for x in per]
                    results.append({
                        "key": r["key"], "chain": r["chain"], "dex": r["dex"], "pair": r["pair"],
                        "fee_pct": r["fee_pct"], "category": r["category"], "tvl": r["tvl"],
                        "mode": label, "width_pct": None if w is None else w * 100,
                        "net_w1": nets[0], "net_w2": nets[1], "net_w3": nets[2],
                        "net_mean": st.mean(nets), "net_min": min(nets),
                        "n_positive": sum(1 for x in nets if x > 0),
                        "income_apr": st.mean(x["fee_apr"] + x["reward_apr"] for x in per),
                        "usd_apr": st.mean(x["net_usd_apr"] for x in per),
                        "hodl_usd_apr": st.mean(x["hodl_usd_apr"] for x in per),
                        "time_in_range": st.mean(x["time_in_range"] for x in per),
                        "rebalances": sum(x["rebalances"] for x in per),
                    })
            # дельта-нейтральная LP: только пары X/стейбл, где X — ETH или BTC
            if r["category"] == "Голубая фишка/стейбл":
                risky = r["class1"] if quote_is_0 else r["class0"]
                for label, reb, earn, rew in modes:
                    for w in (0.10, 0.20, 0.30, None):
                        if w is None and reb:
                            continue
                        per = []
                        for a, b in wins + [(h[0]["timestamp"], h[-1]["timestamp"])]:
                            res = backtest_hedged(slice_hist(h, a, b), L_h, r["tvl"], w, r["fee_pct"],
                                                  invert=quote_is_0, funding=funding[risky], rebalance=reb,
                                                  reward_usd_day=rew, earn_fees=earn,
                                                  staked_share=min(1.0, max(0.05, share)))
                            if res:
                                per.append(res)
                        if len(per) < 4:
                            continue
                        hedged.append({
                            "key": r["key"], "chain": r["chain"], "dex": r["dex"], "pair": r["pair"],
                            "fee_pct": r["fee_pct"], "tvl": r["tvl"], "mode": label,
                            "width_pct": None if w is None else w * 100,
                            "usd_w1": per[0]["net_usd_apr"], "usd_w2": per[1]["net_usd_apr"],
                            "usd_w3": per[2]["net_usd_apr"], "usd_90d": per[3]["net_usd_apr"],
                            "usd_min": min(x["net_usd_apr"] for x in per[:3]),
                            "fee_apr_90d": per[3]["fee_apr"] + per[3]["reward_apr"],
                            "funding_apr_90d": per[3]["funding_apr"],
                            "price_pnl_apr_90d": per[3]["lp_price_pnl_apr"],
                            "cost_apr_90d": per[3]["cost_apr"],
                        })

    # ---------------- базис-трейд и funding по окнам
    bench = benchmarks()
    lido = next((b["apy"] for b in bench if b["project"] == "lido"), 2.2)
    basis = {}
    for coin, fr in funding.items():
        end = max(fr)
        rows = {}
        for i, (a, b) in enumerate(windows(end)):
            xs = [v for k, v in fr.items() if a < k <= b]
            rows[f"w{i + 1}"] = st.mean(xs) * 24 * 365 * 100 if xs else None
        xs = list(fr.values())
        rows["90d"] = st.mean(xs) * 24 * 365 * 100
        rows["negative_hours_pct"] = sum(1 for v in xs if v < 0) / len(xs) * 100
        basis[coin] = rows
    out = {"generated_at": int(time.time()), "results": results, "hedged": hedged, "basis": basis,
           "lido_apy": lido, "benchmarks": bench, "universe": universe_out}
    save(PROC / "strategies.json", out)
    _csv(PROC / "hold_beaters.csv", sorted(results, key=lambda x: -x["net_min"]))
    _csv(PROC / "hedged_lp.csv", sorted(hedged, key=lambda x: -x["usd_min"]))

    # ---------------- краткая сводка в консоль
    best = defaultdict(list)
    for x in results:
        best[x["key"]].append(x)
    print("\nУстойчиво перекрывают холд (результат > 0 во всех трёх 30-дн окнах), лучшая настройка на пул:")
    rows = []
    for k, xs in best.items():
        b = max(xs, key=lambda x: x["net_min"])
        if b["net_min"] > 0:
            rows.append(b)
    for b in sorted(rows, key=lambda x: -x["net_mean"]):
        w = "полный" if b["width_pct"] is None else f"±{b['width_pct']:g}%"
        print(f"  {b['chain'][:4]} {b['dex'][:20]:20s} {b['pair']:14s} {b['fee_pct']:<7g} {b['mode'][:28]:28s} {w:7s}"
              f" к холду: среднее {b['net_mean']:+6.1f}% мин {b['net_min']:+6.1f}% | доход {b['income_apr']:5.1f}%"
              f" | в $ {b['usd_apr']:+6.1f}%")
    print("\nДельта-нейтральная LP (лучшее по минимуму окон):")
    for x in sorted(hedged, key=lambda x: -x["usd_min"])[:12]:
        w = "полный" if x["width_pct"] is None else f"±{x['width_pct']:g}%"
        print(f"  {x['chain'][:4]} {x['dex'][:20]:20s} {x['pair']:12s} {x['fee_pct']:<7g} {x['mode'][:26]:26s} {w:7s}"
              f" $APR окна {x['usd_w1']:+6.1f}/{x['usd_w2']:+6.1f}/{x['usd_w3']:+6.1f} 90д {x['usd_90d']:+6.1f}"
              f" (комиссии {x['fee_apr_90d']:.1f}, funding {x['funding_apr_90d']:.1f}, цена {x['price_pnl_apr_90d']:+.1f},"
              f" издержки {x['cost_apr_90d']:.1f})")
    print("\nFunding (шорт получает), % годовых:", basis, "| Lido:", lido)
    for b in bench:
        print("  ориентир:", b)


if __name__ == "__main__":
    main()
