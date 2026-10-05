#!/usr/bin/env python3
"""Uniswap v4 (и PancakeSwap Infinity) против v3 на парах ETH/BTC/стейблы во всех сетях Krystal.

Что считается:
  * где есть v4: TVL и объём по сетям (DefiLlama), пулы ETH/BTC/стейблы с TVL ≥ $300k (Krystal);
  * какую долю комиссии получает LP (после протокольной комиссии Uniswap/Pancake) и есть ли хук;
  * доход на единицу ликвидности: fee APR полнодиапазонной позиции и R = fee APR ÷ σ²/8;
  * бэктест на трёх независимых 30-дневных окнах (позиция открывается заново в начале окна);
  * прямое сравнение для позиции ETH/стейбл: сколько комиссий дала бы позиция с заданными
    границами (±10% и ±20% от цены, плюс свои границы через --range) в каждом пуле ETH/стейбл.

Запуск:  python3 v4_analysis.py [--position-usd 10000 --range 2000 3000]
Результат: data/processed/v4_analysis.json, data/processed/v4_pools.csv
"""
from __future__ import annotations

import argparse
import math
import statistics as st
import time
from collections import defaultdict

from analyze import _csv, build_quote_series
from collect import active_liquidity
from lpscan.clean import clean_pool
from lpscan.common import KRYSTAL_BASE, PROC, get_json, krystal_headers, save
from lpscan.metrics import backtest_cl, history_metrics, human_L, slice_hist, value_per_L

CHAINS = {1: "Ethereum", 42161: "Arbitrum", 8453: "Base", 4663: "Robinhood", 56: "BSC", 137: "Polygon",
          10: "Optimism"}
PROTOCOLS = {"uniswapv3": "v3", "uniswapv4": "v4", "pancakev3": "v3", "pancakev4": "v4"}
OK = {"stable", "eth", "btc"}
BAD = {"цена альта не подтверждена", "мёртвый пул: объёма нет", "нет цены у альта", "TVL≠сумме резервов"}
GRID = {  # ширины для бэктеста; «стандартная» — первая в списке, по ней сравниваем v3 и v4
    "Голубая фишка/стейбл": [0.20, 0.10, 0.30, None],
    "ETH/BTC": [0.15, 0.10, 0.20],
    "Стейбл/стейбл": [0.001, 0.0005, 0.0025],
    "Коррелир. (ETH/LST, BTC/BTC)": [0.0025, 0.001, 0.005],
}


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def pair_kind(r):
    c = sorted([r["class0"], r["class1"]])
    if c == ["eth", "stable"]:
        return "ETH/стейбл"
    if c == ["btc", "stable"]:
        return "BTC/стейбл"
    if c == ["btc", "eth"]:
        return "ETH/BTC"
    if c == ["stable", "stable"]:
        return "стейбл/стейбл"
    return "ETH/ETH или BTC/BTC"


def llama_v4_by_chain():
    out = {}
    for slug in ("uniswap-v4", "uniswap-v3", "pancakeswap-infinity"):
        p = get_json(f"https://api.llama.fi/protocol/{slug}", cache_ttl=6 * 3600)
        tvl = {k: v for k, v in (p.get("currentChainTvls") or {}).items()
               if "-" not in k and k not in ("borrowed", "staking", "pool2")}
        vol = {}
        try:
            s = get_json(f"https://api.llama.fi/summary/dexs/{slug}", cache_ttl=6 * 3600,
                         params={"excludeTotalDataChart": "true"})
            for _, row in (s.get("totalDataChartBreakdown") or [])[-30:]:
                for ch, v in row.items():
                    vol[ch] = vol.get(ch, 0) + (sum(v.values()) if isinstance(v, dict) else v)
        except RuntimeError:
            pass
        out[slug] = {"tvl": tvl, "vol30d": vol}
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--position-usd", type=float, default=10000)
    ap.add_argument("--range", type=float, nargs=2, default=None,
                    help="свои границы позиции ETH в $, например --range 2000 3000")
    a = ap.parse_args()
    H = krystal_headers()
    llama = llama_v4_by_chain()
    pools_out, user_cmp = [], []
    end = int(time.time()) // 3600 * 3600
    for cid, cname in CHAINS.items():
        recs = []
        protos = ["uniswapv3", "uniswapv4"] + (["pancakev3", "pancakev4"] if cid in (56, 8453) else [])
        for pr in protos:
            raw = get_json(f"{KRYSTAL_BASE}/v1/pools", headers=H, cache_ttl=3 * 3600, params=dict(
                chainId=cid, protocol=pr, sortBy=1, minTvl=300000, limit=500, includeTokenPrice="true",
                withIncentives="true"))
            if not isinstance(raw, list):
                continue
            for p in raw:
                r = clean_pool(p)
                r["hook"] = (p.get("hook") or "")
                r["gen"] = PROTOCOLS[pr]
                st7 = p.get("stats7d") or {}
                # доля комиссии пула, которая достаётся LP, % (остальное — протокольная комиссия)
                r["lp_fee_share"] = (st7["fee"] / st7["volume"] * 100 / r["fee_pct"] * 100
                                     if st7.get("volume") and r["fee_pct"] else None)
                recs.append(r)
        uni = [r for r in recs if r["class0"] in OK and r["class1"] in OK and r["tvl"] >= 3e5
               and r["vol_7d"] > 0 and not (set(r["flags"]) & BAD)]
        log(f"{cname}: пулов ETH/BTC/стейблы {len(uni)} (v4: {sum(1 for r in uni if r['gen'] == 'v4')})")
        hist = {}
        for r in uni:
            h = get_json(f"{KRYSTAL_BASE}/v1/pools/{cid}/{r['address']}/historical", headers=H, cache_ttl=6 * 3600,
                         params=dict(startTime=end - 90 * 86400, endTime=end))
            if isinstance(h, list) and h:
                hist[r["key"]] = sorted(h, key=lambda x: x["timestamp"])
                r.update(history_metrics(hist[r["key"]]))
        qser = build_quote_series(uni, hist)
        for r in uni:
            tk = get_json(f"{KRYSTAL_BASE}/v1/pools/{cid}/{r['address']}/ticks", headers=H, cache_ttl=6 * 3600,
                          params={"factoryAddress": r["factory"]})
            if not isinstance(tk, list) or not tk:
                continue
            _, Lraw = active_liquidity(tk, r["sqrt_price_x96"])
            if not Lraw or not r["pool_price"] or not r["price1"]:
                continue
            L = human_L(Lraw, r["dec0"], r["dec1"])
            fr_usd = L * 2 * math.sqrt(r["pool_price"]) * r["price1"]
            r["L_active_h"] = L
            r["fee_apr_fullrange"] = r["fee_7d"] / 7 * 365 / fr_usd * 100 if fr_usd else None
            r["fee_apr_fullrange_30d"] = r["fee_30d"] / 30 * 365 / fr_usd * 100 if fr_usd else None
            r["conc_eff"] = fr_usd / r["tvl"] if r["tvl"] else None
            if r.get("lvr_full_pct") and r["fee_apr_fullrange"]:
                r["R"] = r["fee_apr_fullrange"] / max(r["lvr_full_pct"], 0.01)
            # бэктест на трёх окнах
            h = hist.get(r["key"])
            order = {"stable": 0, "eth": 1, "btc": 2}
            quote_is_0 = order[r["class0"]] < order[r["class1"]]
            qcls = r["class0"] if quote_is_0 else r["class1"]
            quote = None if qcls == "stable" else qser.get(qcls)
            if h and h[-1]["timestamp"] - h[0]["timestamp"] > 85 * 86400 and (qcls == "stable" or quote):
                wins = [(h[-1]["timestamp"] - (3 - i) * 30 * 86400, h[-1]["timestamp"] - (2 - i) * 30 * 86400)
                        for i in range(3)]
                grid = GRID.get(r["category"], [0.20])
                res = {}
                for w in grid:
                    per = [backtest_cl(slice_hist(h, x, y), L, r["tvl"], w, r["fee_pct"], invert=quote_is_0,
                                       quote_usd=quote) for x, y in wins]
                    per = [p for p in per if p]
                    if len(per) == 3:
                        res[w] = {"net": [p["net_vs_hodl_apr"] for p in per],
                                  "income": st.mean(p["fee_apr"] for p in per)}
                if res:
                    std_w = grid[0]
                    best_w = max(res, key=lambda w: min(res[w]["net"]))
                    r["bt_std_width"] = None if std_w is None else std_w * 100
                    r["bt_std_net"] = res.get(std_w, {}).get("net")
                    r["bt_std_income"] = res.get(std_w, {}).get("income")
                    r["bt_best_width"] = None if best_w is None else best_w * 100
                    r["bt_best_net"] = res[best_w]["net"]
                    r["bt_best_income"] = res[best_w]["income"]
            # прямое сравнение для позиции ETH/стейбл
            if pair_kind(r) == "ETH/стейбл" and r["tvl"] >= 1e6:
                eth_is_0 = r["class0"] == "eth"
                P = r["pool_price"]                          # token1 за token0
                p_eth = P if eth_is_0 else 1 / P
                rows = {}
                bands = [("±20% от цены", p_eth / 1.2, p_eth * 1.2), ("±10% от цены", p_eth / 1.1, p_eth * 1.1)]
                if a.range:
                    bands.insert(0, ("ваши границы", a.range[0], a.range[1]))
                for label, lo_usd, hi_usd in bands:
                    lo, hi = (lo_usd, hi_usd) if eth_is_0 else (1 / hi_usd, 1 / lo_usd)
                    if not lo <= P <= hi:
                        rows[label] = None
                        continue
                    Lpos = a.position_usd / r["price1"] / value_per_L(P, lo, hi)
                    share = Lpos / (L + Lpos)
                    rows[label] = {"apr_7d": r["fee_7d"] / 7 * share * 365 / a.position_usd * 100,
                                   "apr_30d": r["fee_30d"] / 30 * share * 365 / a.position_usd * 100}
                user_cmp.append({"chain": cname, "gen": r["gen"], "dex": r["dex"], "pair": r["pair"],
                                 "fee_pct": r["fee_pct"], "lp_fee_share": r["lp_fee_share"], "hook": r["hook"],
                                 "tvl": r["tvl"], **{k: v for k, v in rows.items()}})
        for r in uni:
            pools_out.append({k: r.get(k) for k in (
                "key", "chain", "gen", "dex", "protocol", "pair", "category", "fee_pct", "hook", "lp_fee_share",
                "tvl", "vol_tvl_7d_daily", "fee_apr_7d", "fee_apr_30d", "fee_apr_sustained", "reward_apr",
                "fee_apr_fullrange", "fee_apr_fullrange_30d", "conc_eff", "sigma", "lvr_full_pct", "R", "apr_cv",
                "tvl_chg_30d", "hist_days", "bt_std_width", "bt_std_net", "bt_std_income", "bt_best_width",
                "bt_best_net", "bt_best_income", "flags")})
            pools_out[-1]["kind"] = pair_kind(r)
    out = {"generated_at": int(time.time()), "llama": llama, "pools": pools_out, "user_cmp": user_cmp,
           "position_usd": a.position_usd, "range": a.range}
    save(PROC / "v4_analysis.json", out)
    _csv(PROC / "v4_pools.csv", pools_out)

    # ---------------- сводка
    print("\n=== Uniswap v4 по сетям (DefiLlama): TVL / объём за 30 дней")
    v4 = llama["uniswap-v4"]
    for ch, v in sorted(v4["tvl"].items(), key=lambda kv: -kv[1])[:12]:
        print(f"  {ch:16s} TVL ${v / 1e6:8,.1f}M  объём 30д ${v4['vol30d'].get(ch, 0) / 1e9:6.2f}B")
    print("\n=== v3 против v4: лучшие пулы по типу пары и сети (TVL ≥ $1M)")
    g = defaultdict(list)
    for r in pools_out:
        if r["tvl"] >= 1e6:
            g[(r["chain"], r["kind"])].append(r)
    for (ch, kind), rows in sorted(g.items()):
        print(f"  — {ch} · {kind}")
        for r in sorted(rows, key=lambda r: -(r["tvl"]))[:6]:
            def f(x, n=1):
                return "—" if x is None else f"{x:.{n}f}"
            net = "/".join(f"{v:+.0f}" for v in r["bt_std_net"]) if r.get("bt_std_net") else "—"
            print(f"     {r['gen']} {r['dex'][:16]:16s} {r['pair']:13s} {r['fee_pct']:<7g} хук={'да' if r['hook'] and int(r['hook'], 16) else 'нет'}"
                  f" LP доля {f(r['lp_fee_share'], 0)}% TVL ${r['tvl'] / 1e6:6.1f}M fee30 {f(r['fee_apr_30d'])}%"
                  f" fullrange {f(r['fee_apr_fullrange'], 2)}% R {f(r['R'], 2)}"
                  f" | бэктест ±{r.get('bt_std_width')}%: {net} (доход {f(r.get('bt_std_income'))}%)")
    print(f"\n=== Позиция ${a.position_usd:,.0f} ETH/стейбл: fee APR по 7д / 30д")
    for u in sorted(user_cmp, key=lambda u: -((u.get("±20% от цены") or {}).get("apr_30d") or 0)):
        def g2(k):
            v = u.get(k)
            if k not in u:
                return "—"
            return "вне диапазона" if v is None else f"{v['apr_7d']:5.1f}/{v['apr_30d']:5.1f}%"
        print(f"  {u['chain']:9s} {u['gen']} {u['dex'][:16]:16s} {u['pair']:12s} {u['fee_pct']:<7g} TVL ${u['tvl'] / 1e6:6.1f}M"
              f" | свои границы {g2('ваши границы')} | ±20% {g2('±20% от цены')} | ±10% {g2('±10% от цены')}")


if __name__ == "__main__":
    main()
