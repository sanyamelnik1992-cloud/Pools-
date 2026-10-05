#!/usr/bin/env python3
"""Анализ собранных данных: метрики доходности/риска, сверка источников, бэктесты стратегий.

Запуск:  python3 analyze.py   (после collect.py)
Результат: data/processed/report_data.json, data/processed/pools.csv, backtests.csv
"""
from __future__ import annotations

import csv
import math
import statistics as st
from collections import defaultdict

from lpscan.clean import clean_pool
from lpscan.common import CHAINS, PROC, RAW, load, save
from lpscan.metrics import backtest_cl, history_metrics, human_L

LLAMA_PROJECT = {
    "uniswapv2": "uniswap-v2", "uniswapv3": "uniswap-v3", "uniswapv4": "uniswap-v4",
    "aerodromecl": "aerodrome-slipstream", "aerodromecl2": "aerodrome-slipstream",
    "aerodromecl3": "aerodrome-slipstream", "aerodrome": "aerodrome-v1",
    "camelotv3": "camelot-v3", "camelotv2": "camelot-v2", "sushiv3": "sushiswap-v3",
    "sushiv2": "sushiswap", "pancakev3": "pancakeswap-amm-v3", "pancakev2": "pancakeswap-amm",
    "pancakev4": "pancakeswap-infinity",
}
WIDTHS = {
    "Стейбл/стейбл": [0.0005, 0.001, 0.0025, 0.005, 0.01, None],
    "Коррелир. (ETH/LST, BTC/BTC)": [0.001, 0.0025, 0.005, 0.01, 0.02, None],
}
WIDTHS_DEFAULT = [0.01, 0.025, 0.05, 0.10, 0.20, 0.40, None]


def r2(x, n=2):
    return None if x is None or (isinstance(x, float) and math.isnan(x)) else round(x, n)


def risk_score(r: dict) -> int:
    s = 0
    sig = r.get("sigma")
    if sig is None:
        s += 2
    else:
        s += 0 if sig < 0.05 else 1 if sig < 0.3 else 2 if sig < 0.6 else 3 if sig < 0.9 else 4
    tvl = r["tvl"]
    s += 0 if tvl >= 10e6 else 1 if tvl >= 1e6 else 2 if tvl >= 250e3 else 3
    cv = r.get("apr_cv")
    s += 1 if cv is None else 0 if cv < 0.4 else 1 if cv < 0.8 else 2
    if (r.get("tvl_chg_30d") or 0) < -30:
        s += 1
    if r["category"] == "Альт/мем (long-tail)":
        s += 1
    s += min(2, len(r["flags"]))
    if (r.get("hist_days") or 0) < 30:
        s += 1
    return max(1, min(10, s))


def red_flags(r: dict) -> list[str]:
    f = list(r["flags"])
    if r["fee_apr_24h"] > 100 and r["tvl"] < 250_000:
        f.append("высокий APR на микро-TVL")
    if r["fee_apr_30d"] > 0 and r["fee_apr_24h"] / r["fee_apr_30d"] > 4:
        f.append("разовый всплеск объёма")
    if r["vol_tvl_24h"] > 10 and r["fee_pct"] >= 0.03:  # у стейблов с комиссией 0.001% оборот 10x — норма
        f.append("объём/TVL > 10 (wash/MEV?)")
    if (r.get("hist_days") or 99) < 21:
        f.append("пул моложе 3 недель")
    if (r.get("tvl_chg_30d") or 0) < -50:
        f.append("отток TVL > 50% за 30д")
    if r["reward_apr"] > 3 * max(r["fee_apr_30d"], 1) and r["reward_apr"] > 20:
        f.append("доход держится на эмиссии")
    eur = {"EURC", "EURA", "EURE"}
    fx_pair = (r["token0"].upper() in eur) != (r["token1"].upper() in eur)  # EUR/USD — курс, а не депег
    if r["category"] in ("Стейбл/стейбл", "Коррелир. (ETH/LST, BTC/BTC)") and not fx_pair and \
            (r.get("price_dev_max") or 0) > 2:
        f.append(f"депег: отклонение цены до {r['price_dev_max']:.0f}% за 90д")
    if r.get("xcheck_tvl_dev") is not None and abs(r["xcheck_tvl_dev"]) > 50:
        f.append("TVL расходится с GeckoTerminal")
    return f


def build_quote_series(recs, hist):
    """USD-цена ETH и BTC по часам из крупнейших пулов X/USDC сети (для бэктестов в USD)."""
    out = {}
    for cls in ("eth", "btc"):
        cand = [r for r in recs if r["key"] in hist and {r["class0"], r["class1"]} == {cls, "stable"}]
        if not cand:
            continue
        best = max(cand, key=lambda r: r["tvl"])
        inv = best["class0"] == "stable"
        ser = {x["timestamp"]: (1 / x["poolPrice"] if inv else x["poolPrice"]) for x in hist[best["key"]]
               if x.get("poolPrice")}
        ser["last"] = list(ser.values())[-1]
        out[cls] = ser
    return out


def main():
    meta = load(RAW / "meta.json")
    gecko = load(RAW / "gecko_pools.json")
    dexs = load(RAW / "dexscreener_pools.json")
    rpc = load(RAW / "rpc_checks.json")
    llama_pools = load(RAW / "llama_pools.json")
    llama_charts = load(RAW / "llama_charts.json")
    aero_staked = rpc.get("aero_staked", {})

    all_recs, backtests, liq_profiles = [], [], {}
    llama_index = defaultdict(list)
    for lp in llama_pools:
        toks = tuple(sorted(t.lower() for t in (lp.get("underlyingTokens") or [])))
        llama_index[(lp["chain"], lp["project"], toks)].append(lp)

    for cid, cinfo in CHAINS.items():
        pools = load(RAW / f"krystal_pools_{cid}.json")
        hist = load(RAW / f"krystal_history_{cid}.json")
        ticks = load(RAW / f"krystal_ticks_{cid}.json")
        recs = [clean_pool(p) for p in pools]
        for r in recs:
            if r["key"] in hist:
                r.update(history_metrics(hist[r["key"]]))
            g = gecko.get(r["key"])
            if g:
                r["gecko_tvl"], r["gecko_vol_24h"] = g["reserve_usd"], g["vol_24h"]
                r["gecko_created"] = g.get("created")
                if g["reserve_usd"] > 0:
                    r["xcheck_tvl_dev"] = (r["tvl"] / g["reserve_usd"] - 1) * 100
                if g["vol_24h"] > 0:
                    r["xcheck_vol_dev"] = (r["vol_24h"] / g["vol_24h"] - 1) * 100
            d = dexs.get(r["key"])
            if d:
                r["dexs_tvl"], r["dexs_vol_24h"] = d.get("liquidity_usd"), d.get("vol_24h")
            if r["key"] in rpc:
                r["rpc_usd"] = rpc[r["key"]]["onchain_usd"]
            if r["key"] in aero_staked:
                r["staked_share"] = aero_staked[r["key"]]["share"]
                if r["staked_share"]:
                    r["reward_apr_staked"] = r["reward_apr"] / max(0.05, r["staked_share"])
            # DefiLlama: сопоставляем по набору токенов и проекту, берём ближайший по TVL
            toks = tuple(sorted([r["token0_addr"], r["token1_addr"]]))
            cands = llama_index.get((cinfo["llama"], LLAMA_PROJECT.get(r["protocol"], "?"), toks), [])
            if cands:
                lp = min(cands, key=lambda c: abs(math.log((c["tvlUsd"] + 1) / (r["tvl"] + 1))))
                if abs(math.log((lp["tvlUsd"] + 1) / (r["tvl"] + 1))) < math.log(3):
                    r.update(llama_id=lp["pool"], llama_tvl=lp["tvlUsd"], llama_apy_base=lp.get("apyBase"),
                             llama_apy_reward=lp.get("apyReward"), llama_apy_mean30=lp.get("apyMean30d"),
                             llama_reward_tokens=lp.get("rewardTokens"))
            # Активная ликвидность → APR полнодиапазонной позиции и эффективная концентрация
            t = ticks.get(r["key"])
            if t and t.get("active_L") not in (None, "None") and r["pool_price"] and r["price1"]:
                Lh = human_L(int(t["active_L"]), r["dec0"], r["dec1"])
                fr_usd = Lh * 2 * math.sqrt(r["pool_price"]) * r["price1"]
                if fr_usd > 0:
                    r["L_active_h"] = Lh
                    r["fee_apr_fullrange"] = r["fee_7d"] / 7 * 365 / fr_usd * 100
                    r["conc_eff"] = fr_usd / r["tvl"] if r["tvl"] else None
                    liq_profiles[r["key"]] = t
            elif not r["is_cl"]:
                r["fee_apr_fullrange"] = r["fee_apr_7d"]
                r["conc_eff"] = 1.0
            # Устойчивая fee APR = минимум из 7д, 30д и медианы дневных значений за 90д
            vals = [v for v in (r["fee_apr_7d"], r["fee_apr_30d"], r.get("apr_daily_median")) if v]
            r["fee_apr_sustained"] = min(vals) if vals else 0.0
            lvr = r.get("lvr_full_pct")
            if lvr is not None and r.get("fee_apr_fullrange"):
                r["fee_lvr_ratio"] = r["fee_apr_fullrange"] / max(lvr, 0.01)
                r["net_apr_avg_lp"] = r["fee_apr_sustained"] - (r.get("conc_eff") or 1) * lvr
            r["risk"] = risk_score(r)
            r["red_flags"] = red_flags(r)
        # CE по категориям — для пулов без тиков (приближённая оценка, помечается «≈»)
        ce_med = defaultdict(list)
        for r in recs:
            if r.get("conc_eff") and r["is_cl"]:
                ce_med[(r["category"], r["dex"])].append(r["conc_eff"])
                ce_med[r["category"]].append(r["conc_eff"])
        for r in recs:
            if "net_apr_avg_lp" not in r and r.get("lvr_full_pct") is not None:
                ce_list = ce_med.get((r["category"], r["dex"])) or ce_med.get(r["category"]) or [1.0]
                ce = st.median(ce_list) if r["is_cl"] else 1.0
                r["net_apr_avg_lp"] = r["fee_apr_sustained"] - ce * r["lvr_full_pct"]
                r["net_apr_approx"] = True
            if r.get("net_apr_avg_lp") is not None:
                r["ret_per_risk"] = r["net_apr_avg_lp"] / r["risk"]
        all_recs += recs

        # ---------------- бэктесты стратегий
        quote_series = build_quote_series(recs, hist)
        cands = [r for r in recs if r["key"] in hist and r.get("L_active_h") and not r["red_flags"]
                 and r.get("hist_days", 0) > 60]
        chosen = []
        for cat in ("Голубая фишка/стейбл", "ETH/BTC", "Коррелир. (ETH/LST, BTC/BTC)", "Стейбл/стейбл",
                    "Крупный альт"):
            c = sorted([r for r in cands if r["category"] == cat], key=lambda r: -r["fee_7d"])
            chosen += c[:2]
        aero = sorted([r for r in cands if r["dex"] == "Aerodrome Slipstream" and r["reward_usd_day"] > 0],
                      key=lambda r: -r["reward_usd_day"])[:3]
        chosen += [a for a in aero if a not in chosen]
        for r in chosen:
            c0, c1 = r["class0"], r["class1"]
            # котируемый токен: стейбл > ETH > BTC
            order = {"stable": 0, "eth": 1, "btc": 2, "major": 3, "stock": 4, "alt": 5}
            quote_is_0 = order[c0] < order[c1]
            qcls = c0 if quote_is_0 else c1
            qser = None if qcls == "stable" else quote_series.get(qcls)
            if qcls not in ("stable",) and qser is None:
                continue
            widths = WIDTHS.get(r["category"], WIDTHS_DEFAULT)
            modes = [("Пассивно", False, True, 0.0), ("Авто-ребаланс", True, True, 0.0)]
            if r["dex"] == "Aerodrome Slipstream" and r["reward_usd_day"] > 0:
                modes += [("Стейк (AERO), пассивно", False, False, r["reward_usd_day"]),
                          ("Стейк (AERO), авто-ребаланс", True, False, r["reward_usd_day"])]
            share = (aero_staked.get(r["key"]) or {}).get("share") or 1.0
            for window in (90, 30):
                for label, reb, earn, rew in modes:
                    for w in widths:
                        if w is None and reb:
                            continue
                        res = backtest_cl(hist[r["key"]], r["L_active_h"], r["tvl"], w, r["fee_pct"],
                                          invert=quote_is_0, quote_usd=qser, rebalance=reb,
                                          reward_usd_day=rew, earn_fees=earn, window_days=window,
                                          staked_share=min(1.0, max(0.05, share)))
                        if res:
                            backtests.append({"key": r["key"], "chain": r["chain"], "dex": r["dex"],
                                              "pair": r["pair"], "fee_pct": r["fee_pct"],
                                              "category": r["category"], "tvl": r["tvl"], "mode": label,
                                              "window": window, "staked_share": share,
                                              "width_pct": None if w is None else w * 100, **res})

    # ---------------- агрегаты
    def agg(rows):
        tvl = sum(r["tvl"] for r in rows)
        f7 = sum(r["fee_7d"] for r in rows)
        rew = sum(r["reward_usd_day"] for r in rows)
        big = [r["fee_apr_sustained"] for r in rows if r["tvl"] >= 1e6]
        return {"n": len(rows), "tvl": tvl, "fee_7d": f7, "vol_7d": sum(r["vol_7d"] for r in rows),
                "w_fee_apr": f7 / 7 * 365 / tvl * 100 if tvl else 0,
                "w_reward_apr": rew * 365 / tvl * 100 if tvl else 0,
                "median_apr_big": st.median(big) if big else None, "n_big": len(big)}

    clean = [r for r in all_recs if "цена альта не подтверждена" not in r["flags"]]
    by_dex = []
    for (chain, dex), rows in sorted(_group(clean, ("chain", "dex")).items()):
        a = agg(rows)
        top = sorted([r for r in rows if r["tvl"] >= 500e3], key=lambda r: -r["fee_apr_sustained"])[:3]
        a.update(chain=chain, dex=dex, top_pairs=[f"{r['pair']} {r['fee_pct']:g}%" for r in top])
        by_dex.append(a)
    by_cat = []
    for (chain, cat), rows in sorted(_group(clean, ("chain", "category")).items()):
        a = agg(rows)
        sig = [r["sigma"] for r in rows if r.get("sigma") is not None and r["tvl"] >= 250e3]
        net = [r["net_apr_avg_lp"] for r in rows if r.get("net_apr_avg_lp") is not None and r["tvl"] >= 250e3]
        ratio = [r["fee_lvr_ratio"] for r in rows if r.get("fee_lvr_ratio") and r["tvl"] >= 250e3]
        a.update(chain=chain, category=cat, median_sigma=st.median(sig) if sig else None,
                 median_net=st.median(net) if net else None,
                 median_fee_lvr=st.median(ratio) if ratio else None)
        by_cat.append(a)

    # ---------------- сводка по сетям (DefiLlama) и Blast
    chains_ov = {}
    for ch in ("Arbitrum", "Base", "Blast"):
        dx, fe = load(RAW / f"llama_dexs_{ch}.json"), load(RAW / f"llama_fees_{ch}.json")
        chart = [[t, v] for t, v in (dx.get("totalDataChart") or [])][-120:]
        prot = sorted(dx.get("protocols", []), key=lambda p: -(p.get("total30d") or 0))[:10]
        chains_ov[ch] = {"vol_24h": dx.get("total24h"), "vol_30d": dx.get("total30d"),
                         "vol_change_30d": dx.get("change_30dover30d"), "fees_30d": fe.get("total30d"),
                         "fees_24h": fe.get("total24h"), "chart": chart,
                         "top_dex": [{"name": p.get("displayName") or p["name"], "vol_30d": p.get("total30d"),
                                      "vol_24h": p.get("total24h")} for p in prot]}
    for ch in ("Arbitrum", "Base", "Blast"):
        lp = [p for p in llama_pools if p["chain"] == ch and p.get("exposure") == "multi"]
        chains_ov[ch]["llama_lp_tvl"] = sum(p["tvlUsd"] for p in lp)
        chains_ov[ch]["llama_lp_n"] = len(lp)
    blast_pools = load(RAW / "gecko_blast_pools.json")
    for p in blast_pools:
        p["fee_apr_est"] = None
        name = p["name"]
        fee = None
        for tok in name.split():
            if tok.endswith("%"):
                try:
                    fee = float(tok[:-1])
                except ValueError:
                    pass
        if fee and p["reserve_usd"] > 0:
            p["fee_apr_est"] = p["vol_24h"] * fee / 100 * 365 / p["reserve_usd"] * 100
    chains_ov["Blast"]["pools"] = sorted(blast_pools, key=lambda p: -p["reserve_usd"])[:15]
    chains_ov["Blast"]["n_pools_over_100k"] = sum(1 for p in blast_pools if p["reserve_usd"] > 1e5)
    chains_ov["Blast"]["rpc_block"] = rpc.get("blast_block")

    # ---------------- DefiLlama: пулы, которых нет у Krystal (Aerodrome basic, Camelot, Curve, Fluid...)
    llama_dex = []
    for p in llama_pools:
        if p["chain"] in ("Arbitrum", "Base") and p.get("exposure") == "multi" and p["tvlUsd"] >= 1e6 \
                and p["project"] not in ("gmx-v2-perps", "beefy", "morpho-blue", "stake-dao-yield"):
            llama_dex.append({k: p.get(k) for k in ("chain", "project", "symbol", "tvlUsd", "apyBase", "apyReward",
                                                     "apyMean30d", "ilRisk", "volumeUsd1d", "poolMeta", "pool",
                                                     "stablecoin", "rewardTokens")})
    # история APY из DefiLlama (база vs награды) для графика стабильности
    llama_hist = {}
    for p in llama_dex:
        ch = llama_charts.get(p["pool"])
        if ch:
            llama_hist[p["pool"]] = [[c["timestamp"][:10], r2(c.get("apyBase")), r2(c.get("apyReward")),
                                      round(c.get("tvlUsd") or 0)] for c in ch]

    # ---------------- сверка источников (топ пулов по TVL)
    xcheck = []
    for r in sorted([r for r in clean if r.get("gecko_tvl")], key=lambda r: -r["tvl"])[:30]:
        xcheck.append({k: r.get(k) for k in ("chain", "dex", "pair", "fee_pct", "tvl", "gecko_tvl", "dexs_tvl",
                                             "llama_tvl", "rpc_usd", "vol_24h", "gecko_vol_24h", "dexs_vol_24h",
                                             "fee_apr_7d", "llama_apy_base", "llama_apy_reward",
                                             "xcheck_tvl_dev")})
    rpc_liq = [{**v, "key": k} for k, v in rpc.items() if isinstance(v, dict)]

    # ---------------- выгрузка
    keep = ["key", "chain", "dex", "protocol", "address", "pair", "token0", "token1", "fee_pct", "category",
            "tvl", "tvl_reported", "vol_24h", "vol_7d", "fee_7d", "fee_apr_24h", "fee_apr_7d", "fee_apr_30d",
            "apr_daily_median", "apr_daily_p10", "fee_apr_sustained", "reward_apr", "reward_usd_day",
            "reward_tokens", "staked_share", "reward_apr_staked", "llama_apy_base", "llama_apy_reward", "vol_tvl_24h", "vol_tvl_7d_daily",
            "sigma", "sigma_hourly", "lvr_full_pct", "fee_apr_fullrange", "conc_eff", "fee_lvr_ratio", "net_apr_avg_lp",
            "net_apr_approx", "ret_per_risk", "risk", "apr_cv", "tvl_cv", "tvl_chg_30d", "tvl_chg_90d",
            "price_chg_30d", "price_chg_90d", "il_30d", "il_90d", "price_max_dd", "price_dev_max", "price_min", "price_max", "hist_days", "gecko_tvl",
            "xcheck_tvl_dev", "red_flags", "is_cl"]
    pools_out = []
    for r in all_recs:
        o = {k: r.get(k) for k in keep}
        for k, v in o.items():
            if isinstance(v, float):
                o[k] = round(v, 4)
        pools_out.append(o)
    series = {r["key"]: {"apr": r.get("apr_series"), "tvl": r.get("tvl_series")} for r in all_recs
              if r.get("apr_series") and r["tvl"] >= 1e6 and not r["red_flags"]}
    profiles = {}
    for k, t in liq_profiles.items():
        r = next(x for x in all_recs if x["key"] == k)
        if r["tvl"] >= 3e6:
            profiles[k] = {"tick": t["tick"], "profile": t["profile"], "dec0": r["dec0"], "dec1": r["dec1"]}

    out = {"meta": meta, "pools": pools_out, "by_dex": by_dex, "by_cat": by_cat, "chains": chains_ov,
           "backtests": backtests, "llama_dex": llama_dex, "llama_hist": llama_hist, "xcheck": xcheck,
           "rpc": rpc_liq, "series": series, "profiles": profiles}
    save(PROC / "report_data.json", out)
    _csv(PROC / "pools.csv", pools_out)
    _csv(PROC / "backtests.csv", backtests)
    print(f"pools: {len(pools_out)}, backtests: {len(backtests)}, dex groups: {len(by_dex)}")


def _group(rows, keys):
    g = defaultdict(list)
    for r in rows:
        g[tuple(r[k] for k in keys)].append(r)
    return g


def _csv(path, rows):
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    cols = list(rows[0].keys())
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: ("; ".join(map(str, v)) if isinstance(v, list) else v) for k, v in r.items()})


if __name__ == "__main__":
    main()
