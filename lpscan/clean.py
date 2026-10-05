"""Очистка снапшотов Krystal: проверка TVL по ценам токенов, флаги подозрительных пулов."""
from __future__ import annotations

from .common import pair_category, token_class

V3_LIKE = {"uniswapv3", "pancakev3", "sushiv3", "camelotv3", "aerodromecl", "aerodromecl2",
           "aerodromecl3", "uniswapv4", "pancakev4"}


def pool_key(p: dict) -> str:
    return f"{p['chain']['id']}:{p['poolAddress'].lower()}"


def clean_pool(p: dict) -> dict:
    """Плоская запись пула + исправленный TVL + флаги."""
    t0, t1 = p["token0"], p["token1"]
    s0, s1 = t0["token"]["symbol"], t1["token"]["symbol"]
    n0, n1 = t0["token"].get("name"), t1["token"].get("name")
    c0, c1 = token_class(s0, n0), token_class(s1, n1)
    v0, v1 = t0.get("value"), t1.get("value")
    tvl = p.get("tvl") or 0.0
    flags = []
    tvl_clean = tvl
    # нет цены у одной стороны: если вторая сторона «доверенная», TVL ≈ 2× её стоимость
    for vi, vj, ci, cj in ((v0, v1, c0, c1), (v1, v0, c1, c0)):
        if vi is not None and not vj and ci != "alt" and cj == "alt" and tvl > 2.5 * vi + 1000:
            flags.append("нет цены у альта")
            tvl_clean = min(tvl_clean, 2 * vi)
    if v0 is not None and v1 is not None:
        tsum = v0 + v1
        if tvl > 0 and abs(tsum - tvl) / tvl > 0.25:
            flags.append("TVL≠сумме резервов")
            tvl_clean = min(tvl, tsum)
        trusted = {0: c0 != "alt", 1: c1 != "alt"}
        vals = {0: v0, 1: v1}
        for i, j in ((0, 1), (1, 0)):
            # «доверенная» сторона (стейбл/ETH/BTC/крупный альт) мала, а альт-сторона огромна —
            # типичный признак накрученной цены малоликвидного токена.
            if trusted[i] and not trusted[j] and vals[j] > 10 * max(vals[i], 1.0):
                flags.append("цена альта не подтверждена")
                tvl_clean = min(tvl_clean, 2 * vals[i])
    if c0 == "alt" and c1 == "alt":
        flags.append("обе стороны — альты")
    inc = p.get("incentives") or []
    reward_usd_day = sum(i.get("dailyRewardUsd") or 0 for i in inc)
    reward_apr = reward_usd_day * 365 / tvl_clean * 100 if tvl_clean > 0 else 0.0
    st = {k: p.get(f"stats{k}") or {} for k in ("1h", "24h", "7d", "30d")}

    def apr_from(k, days):
        fee = st[k].get("fee") or 0
        return fee / days * 365 / tvl_clean * 100 if tvl_clean > 0 else 0.0

    proto = p["protocol"]["key"]
    rec = {
        "key": pool_key(p),
        "chain_id": p["chain"]["id"],
        "chain": p["chain"]["name"],
        "address": p["poolAddress"].lower(),
        "protocol": proto,
        "protocol_name": p["protocol"]["name"],
        "factory": p["protocol"].get("factoryAddress"),
        "dex": dex_family(proto),
        "fee_tier": p.get("feeTier"),
        "fee_pct": (p.get("feeTier") or 0) / 1e4,  # 500 -> 0.05 (%)
        "tick_spacing": p.get("tickSpacing"),
        "sqrt_price_x96": p.get("currentSqrtPriceX96"),
        "pool_price": p.get("poolPrice"),
        "token0": s0, "token1": s1,
        "token0_addr": t0["token"]["address"].lower(), "token1_addr": t1["token"]["address"].lower(),
        "dec0": t0["token"]["decimals"], "dec1": t1["token"]["decimals"],
        "price0": t0.get("price"), "price1": t1.get("price"),
        "pair": f"{s0}/{s1}",
        "category": pair_category(s0, s1, n0, n1),
        "class0": c0, "class1": c1,
        "tvl_reported": tvl,
        "tvl": tvl_clean,
        "vol_24h": st["24h"].get("volume") or 0, "vol_7d": st["7d"].get("volume") or 0,
        "vol_30d": st["30d"].get("volume") or 0,
        "fee_24h": st["24h"].get("fee") or 0, "fee_7d": st["7d"].get("fee") or 0,
        "fee_30d": st["30d"].get("fee") or 0,
        "fee_apr_24h": apr_from("24h", 1), "fee_apr_7d": apr_from("7d", 7),
        "fee_apr_30d": apr_from("30d", 30),
        "fee_apr_24h_krystal": st["24h"].get("apr"),
        "reward_usd_day": reward_usd_day,
        "reward_apr": reward_apr,
        "reward_tokens": sorted({i["token"]["symbol"] for i in inc if i.get("token")}),
        "is_cl": proto in V3_LIKE,
        "flags": flags,
    }
    if tvl_clean > 2e6 and rec["vol_7d"] < 0.001 * tvl_clean:
        flags.append("мёртвый пул: объёма нет")
    rec["vol_tvl_24h"] = rec["vol_24h"] / tvl_clean if tvl_clean else 0
    rec["vol_tvl_7d_daily"] = rec["vol_7d"] / 7 / tvl_clean if tvl_clean else 0
    return rec


def dex_family(proto: str) -> str:
    m = {
        "uniswapv2": "Uniswap v2", "uniswapv3": "Uniswap v3", "uniswapv4": "Uniswap v4",
        "pancakev2": "Pancake v2", "pancakev3": "Pancake v3", "pancakev4": "Pancake Infinity",
        "sushiv2": "Sushi v2", "sushiv3": "Sushi v3",
        "camelotv2": "Camelot v2", "camelotv3": "Camelot v3",
        "aerodrome": "Aerodrome v2 (basic)", "aerodromecl": "Aerodrome Slipstream",
        "aerodromecl2": "Aerodrome Slipstream", "aerodromecl3": "Aerodrome Slipstream",
    }
    return m.get(proto, proto)
