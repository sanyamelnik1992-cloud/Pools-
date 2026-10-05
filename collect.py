#!/usr/bin/env python3
"""Сбор данных по LP-пулам Arbitrum и Base (Krystal) + перекрёстная проверка
(DefiLlama, GeckoTerminal, DexScreener, RPC) + Blast для сравнения.

Запуск:  KRYSTAL_CLOUD_KEY=... python3 collect.py [--fresh] [--max-history N]
Результат: data/raw/*.json
"""
from __future__ import annotations

import argparse
import math
import time

from lpscan.clean import clean_pool
from lpscan.common import (BLAST, CHAINS, GECKO, KRYSTAL_BASE, LLAMA_API, LLAMA_YIELDS, RAW,
                           get_json, krystal_headers, rpc_call, save)

DEX_PROJECTS = {  # проекты DefiLlama, которые сопоставляем с Krystal
    "uniswap-v2", "uniswap-v3", "uniswap-v4", "aerodrome-slipstream", "aerodrome-v1",
    "camelot-v2", "camelot-v3", "sushiswap", "sushiswap-v3", "pancakeswap-amm",
    "pancakeswap-amm-v3", "pancakeswap-infinity", "curve-dex", "fluid-dex", "balancer-v2",
    "balancer-v3", "thruster-v3", "thruster-v2", "blasterswap", "fenix-finance", "ambient",
}


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


# ---------------------------------------------------------------- Krystal
def krystal_snapshot(ttl: float):
    H = krystal_headers()
    chains = get_json(f"{KRYSTAL_BASE}/v1/chains", cache_ttl=ttl)
    protocols = get_json(f"{KRYSTAL_BASE}/v1/protocols", cache_ttl=ttl)
    save(RAW / "krystal_chains.json", chains)
    save(RAW / "krystal_protocols.json", protocols)
    out = {}
    for ch in chains:
        if ch["id"] not in CHAINS:
            continue
        pools = {}
        for proto in ch.get("supportedProtocols", []):
            offset = 0
            while True:
                batch = get_json(f"{KRYSTAL_BASE}/v1/pools", headers=H, cache_ttl=ttl, params=dict(
                    chainId=ch["id"], protocol=proto, sortBy=1, minTvl=10000, limit=1000,
                    offset=offset, withIncentives="true", includeTokenPrice="true"))
                if not isinstance(batch, list):
                    log("  ! krystal", proto, batch)
                    break
                for p in batch:
                    pools[(p["poolAddress"].lower(), proto)] = p
                if len(batch) < 1000:
                    break
                offset += 1000
            log(f"Krystal {ch['name']:9s} {proto:14s} pools(TVL≥10k): "
                f"{sum(1 for k in pools if k[1] == proto)}")
        out[ch["id"]] = list(pools.values())
        save(RAW / f"krystal_pools_{ch['id']}.json", out[ch["id"]])
    return out


def select_for_history(pools: list[dict], max_n: int) -> list[dict]:
    recs = [clean_pool(p) for p in pools]
    ok = [r for r in recs if r["tvl"] >= 100_000 and r["fee_7d"] > 0]
    by_fee = sorted(ok, key=lambda r: -r["fee_7d"])[: max_n * 2 // 3]
    by_tvl = sorted(ok, key=lambda r: -r["tvl"])[: max_n // 2]
    by_rew = sorted([r for r in ok if r["reward_apr"] > 0], key=lambda r: -r["reward_usd_day"])[:40]
    sel = {r["key"]: r for r in by_fee + by_tvl + by_rew}
    return list(sel.values())[: max_n]


def krystal_history(chain_id: int, recs: list[dict], ttl: float, days: int = 90):
    H = krystal_headers()
    end = int(time.time()) // 3600 * 3600
    start = end - days * 86400
    hist = {}
    for i, r in enumerate(recs):
        try:
            h = get_json(f"{KRYSTAL_BASE}/v1/pools/{chain_id}/{r['address']}/historical",
                         headers=H, cache_ttl=ttl, params=dict(startTime=start, endTime=end))
        except RuntimeError as e:
            log("  ! history", r["pair"], e)
            continue
        if isinstance(h, list):
            hist[r["key"]] = sorted(h, key=lambda x: x["timestamp"])
        if i % 25 == 0:
            log(f"  history {chain_id}: {i + 1}/{len(recs)}")
    save(RAW / f"krystal_history_{chain_id}.json", hist, compact=True)
    return hist


def active_liquidity(ticks: list[dict], sqrt_price_x96: str) -> tuple[int, int | None]:
    tick = math.floor(2 * math.log(int(sqrt_price_x96) / 2 ** 96) / math.log(1.0001))
    below = [t for t in ticks if t["tickIdx"] <= tick]
    return tick, int(below[-1]["accumulatedLiquidity"] or 0) if below else None


def krystal_ticks(chain_id: int, recs: list[dict], ttl: float, n: int = 45):
    H = krystal_headers()
    base = [r for r in recs if r["is_cl"] and r["tvl"] >= 300_000 and not r["flags"]]
    cand = sorted(base, key=lambda r: -(r["fee_7d"] + r["reward_usd_day"] * 7))[:n]
    # плюс по 4 крупнейших пула каждой категории (стейблы и LST иначе не попадают в выборку)
    for cat in {r["category"] for r in base}:
        cand += sorted([r for r in base if r["category"] == cat], key=lambda r: -r["tvl"])[:4]
    cand = list({r["key"]: r for r in cand}.values())
    out = {}
    for r in cand:
        try:
            t = get_json(f"{KRYSTAL_BASE}/v1/pools/{chain_id}/{r['address']}/ticks", headers=H,
                         cache_ttl=ttl, params={"factoryAddress": r["factory"]})
        except RuntimeError as e:
            log("  ! ticks", r["pair"], e)
            continue
        if not isinstance(t, list) or not t:
            continue
        tick, L = active_liquidity(t, r["sqrt_price_x96"])
        # Профиль ликвидности ±30% от цены — для графика распределения
        prof = [{"tick": x["tickIdx"], "L": int(x["accumulatedLiquidity"] or 0)} for x in t
                if abs(x["tickIdx"] - tick) <= 2700]
        out[r["key"]] = {"tick": tick, "active_L": str(L), "sqrt_price_x96": r["sqrt_price_x96"],
                         "n_ticks": len(t), "profile": prof[::max(1, len(prof) // 400)]}
    save(RAW / f"krystal_ticks_{chain_id}.json", out)
    log(f"  ticks {chain_id}: {len(out)} pools")
    return out


# ---------------------------------------------------------------- DefiLlama
def llama(ttl: float, chart_n: int = 40):
    data = get_json(f"{LLAMA_YIELDS}/pools", cache_ttl=ttl)["data"]
    names = {c["llama"] for c in CHAINS.values()} | {BLAST["llama"]}
    pools = [p for p in data if p["chain"] in names]
    save(RAW / "llama_pools.json", pools)
    log(f"DefiLlama yields: {len(pools)} pools on {sorted(names)}")
    for chain in names:
        for kind in ("dexs", "fees"):
            ov = get_json(f"{LLAMA_API}/overview/{kind}/{chain}", cache_ttl=ttl,
                          params={"excludeTotalDataChartBreakdown": "true"})
            ov.pop("allChains", None)
            ov["totalDataChart"] = (ov.get("totalDataChart") or [])[-400:]
            for pr in ov.get("protocols", []):
                for k in ("methodology", "logo", "linkedProtocols", "chains"):
                    pr.pop(k, None)
            save(RAW / f"llama_{kind}_{chain}.json", ov)
    # история APY по крупнейшим DEX-пулам (база vs награды)
    dex = [p for p in pools if p["project"] in DEX_PROJECTS and p["chain"] != "Blast"
           and p["tvlUsd"] > 1e6 and p.get("exposure") == "multi"]
    top = sorted(dex, key=lambda p: -p["tvlUsd"])[:chart_n]
    top += sorted([p for p in dex if (p.get("apyReward") or 0) > 0],
                  key=lambda p: -p["tvlUsd"])[:chart_n // 2]
    charts = {}
    for p in {p["pool"]: p for p in top}.values():
        try:
            charts[p["pool"]] = get_json(f"{LLAMA_YIELDS}/chart/{p['pool']}", cache_ttl=ttl,
                                         min_interval=0.4)["data"][-120:]
        except RuntimeError as e:
            log("  ! llama chart", e)
    save(RAW / "llama_charts.json", charts)
    log(f"  llama charts: {len(charts)}")


# ---------------------------------------------------------------- GeckoTerminal / DexScreener
def gecko(recs_by_chain: dict[int, list[dict]], ttl: float, n: int = 90):
    out = {}
    for cid, recs in recs_by_chain.items():
        net = CHAINS[cid]["gecko"]
        top = sorted(recs, key=lambda r: -(r["fee_7d"] + r["tvl"] / 100))[:n]
        addrs = [r["address"] for r in top]
        for i in range(0, len(addrs), 30):
            chunk = ",".join(addrs[i:i + 30])
            try:
                d = get_json(f"{GECKO}/networks/{net}/pools/multi/{chunk}", cache_ttl=ttl,
                             min_interval=2.5)
            except RuntimeError as e:
                log("  ! gecko", e)
                continue
            for p in d.get("data", []):
                a = p["attributes"]
                out[f"{cid}:{a['address'].lower()}"] = {
                    "name": a["name"], "reserve_usd": float(a.get("reserve_in_usd") or 0),
                    "vol_24h": float((a.get("volume_usd") or {}).get("h24") or 0),
                    "dex": p["relationships"]["dex"]["data"]["id"],
                    "created": a.get("pool_created_at"),
                }
        log(f"GeckoTerminal {net}: {sum(1 for k in out if k.startswith(str(cid)))} pools")
    save(RAW / "gecko_pools.json", out)
    # Blast — топ пулов по объёму
    blast = []
    for page in (1, 2, 3):
        d = get_json(f"{GECKO}/networks/blast/pools", cache_ttl=ttl, min_interval=2.5,
                     params={"page": page, "sort": "h24_volume_usd_desc"})
        for p in d.get("data", []):
            a = p["attributes"]
            blast.append({"name": a["name"], "address": a["address"],
                          "dex": p["relationships"]["dex"]["data"]["id"],
                          "reserve_usd": float(a.get("reserve_in_usd") or 0),
                          "vol_24h": float((a.get("volume_usd") or {}).get("h24") or 0)})
    save(RAW / "gecko_blast_pools.json", blast)
    log(f"GeckoTerminal blast: {len(blast)} pools")


def dexscreener(recs_by_chain: dict[int, list[dict]], ttl: float, n: int = 30):
    out = {}
    names = {42161: "arbitrum", 8453: "base"}
    for cid, recs in recs_by_chain.items():
        top = [r for r in sorted(recs, key=lambda r: -r["tvl"]) if len(r["address"]) == 42][:n]
        d = get_json(f"https://api.dexscreener.com/latest/dex/pairs/{names[cid]}/"
                     + ",".join(r["address"] for r in top), cache_ttl=ttl, min_interval=1)
        for p in d.get("pairs") or []:
            out[f"{cid}:{p['pairAddress'].lower()}"] = {
                "liquidity_usd": (p.get("liquidity") or {}).get("usd"),
                "vol_24h": (p.get("volume") or {}).get("h24"), "dex": p.get("dexId")}
    save(RAW / "dexscreener_pools.json", out)
    log(f"DexScreener: {len(out)} pools")


# ---------------------------------------------------------------- RPC
SEL_LIQUIDITY = "0x1a686502"        # liquidity()
SEL_STAKED_LIQUIDITY = "0x3ab04b20"  # stakedLiquidity() — Aerodrome Slipstream
SEL_BALANCE_OF = "0x70a08231"       # balanceOf(address)


def rpc_checks(recs_by_chain: dict[int, list[dict]], n: int = 12):
    """Ончейн-проверка: liquidity() пула и реальные балансы токенов на контракте пула.
    Для Aerodrome Slipstream — доля застейканной активной ликвидности (stakedLiquidity/liquidity)."""
    out = {}
    for r in sorted(recs_by_chain.get(8453, []), key=lambda r: -r["reward_usd_day"])[:45]:
        if not r["protocol"].startswith("aerodromecl") or r["reward_usd_day"] <= 0:
            continue
        try:
            liq = int(rpc_call(CHAINS[8453]["rpc"], "eth_call",
                               [{"to": r["address"], "data": SEL_LIQUIDITY}, "latest"]), 16)
            stk = int(rpc_call(CHAINS[8453]["rpc"], "eth_call",
                               [{"to": r["address"], "data": SEL_STAKED_LIQUIDITY}, "latest"]), 16)
            out.setdefault("aero_staked", {})[r["key"]] = {
                "pair": r["pair"], "liquidity": str(liq), "staked": str(stk),
                "share": stk / liq if liq else None}
        except RuntimeError as e:
            log("  ! rpc staked", r["pair"], e)
        time.sleep(0.3)
    for cid, recs in recs_by_chain.items():
        rpc = CHAINS[cid]["rpc"]
        top = [r for r in sorted(recs, key=lambda r: -r["tvl"])
               if r["is_cl"] and len(r["address"]) == 42 and not r["flags"]][:n]
        for r in top:
            try:
                liq = int(rpc_call(rpc, "eth_call", [{"to": r["address"], "data": SEL_LIQUIDITY},
                                                     "latest"]), 16)
                bals = []
                for tok, dec in ((r["token0_addr"], r["dec0"]), (r["token1_addr"], r["dec1"])):
                    data = SEL_BALANCE_OF + r["address"][2:].rjust(64, "0")
                    time.sleep(0.3)
                    bals.append(int(rpc_call(rpc, "eth_call", [{"to": tok, "data": data},
                                                               "latest"]), 16) / 10 ** dec)
                usd = bals[0] * (r["price0"] or 0) + bals[1] * (r["price1"] or 0)
                out[r["key"]] = {"pair": r["pair"], "protocol": r["protocol"], "liquidity": str(liq),
                                 "bal0": bals[0], "bal1": bals[1], "onchain_usd": usd,
                                 "krystal_tvl": r["tvl"]}
            except RuntimeError as e:
                log("  ! rpc", r["pair"], e)
        log(f"RPC {CHAINS[cid]['name']}: {sum(1 for k in out if k.startswith(str(cid)))} pools")
    try:
        bn = int(rpc_call(BLAST["rpc"], "eth_blockNumber", []), 16)
        out["blast_block"] = bn
    except RuntimeError as e:
        log("  ! blast rpc", e)
    save(RAW / "rpc_checks.json", out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fresh", action="store_true", help="игнорировать кэш")
    ap.add_argument("--max-history", type=int, default=220, help="пулов с историей на сеть")
    ap.add_argument("--days", type=int, default=90)
    a = ap.parse_args()
    ttl = 0 if a.fresh else 6 * 3600
    snap = krystal_snapshot(ttl)
    recs_by_chain = {cid: [clean_pool(p) for p in pools] for cid, pools in snap.items()}
    for cid, pools in snap.items():
        sel = select_for_history(pools, a.max_history)
        log(f"{CHAINS[cid]['name']}: история для {len(sel)} пулов")
        krystal_history(cid, sel, ttl, a.days)
        krystal_ticks(cid, recs_by_chain[cid], ttl)
    llama(ttl)
    gecko(recs_by_chain, ttl)
    dexscreener(recs_by_chain, ttl)
    rpc_checks(recs_by_chain)
    save(RAW / "meta.json", {"collected_at": int(time.time()), "days": a.days})
    log("done")


if __name__ == "__main__":
    main()
