#!/usr/bin/env python3
"""Инвентаризация хуков Uniswap v4 и PancakeSwap Infinity во всех сетях Krystal.

Для каждого хука: сеть, адрес, название контракта (Blockscout), права (у Uniswap v4 они закодированы
в младших 14 битах адреса), число пулов, TVL, объём и комиссии за 7 дней, доля TVL в парах ETH/BTC/стейблы,
флаги риска. Для пар ETH/BTC/стейблы — доход позиции ±20% (стейблы ±0.1%) в хуковом пуле против
лучшего пула без хука той же пары и сети.

Запуск:  python3 hooks_analysis.py
Результат: data/processed/hooks.csv, data/processed/hooks_majors.csv, data/processed/hooks.json
"""
from __future__ import annotations

import time
from collections import defaultdict

from analyze import _csv
from collect import active_liquidity
from lpscan.common import KRYSTAL_BASE, PROC, get_json, krystal_headers, save, token_class
from lpscan.metrics import human_L, value_per_L

CHAINS = {1: "Ethereum", 42161: "Arbitrum", 8453: "Base", 10: "Optimism", 137: "Polygon", 56: "BSC",
          43114: "Avalanche", 4663: "Robinhood"}
EXPLORER = {"Ethereum": "https://eth.blockscout.com", "Arbitrum": "https://arbitrum.blockscout.com",
            "Base": "https://base.blockscout.com", "Optimism": "https://optimism.blockscout.com",
            "Polygon": "https://polygon.blockscout.com", "Robinhood": "https://robinhoodchain.blockscout.com"}
# Uniswap v4 Hooks.sol: права хука — биты адреса
FLAGS = [(13, "beforeInitialize"), (12, "afterInitialize"), (11, "beforeAddLiquidity"), (10, "afterAddLiquidity"),
         (9, "beforeRemoveLiquidity"), (8, "afterRemoveLiquidity"), (7, "beforeSwap"), (6, "afterSwap"),
         (5, "beforeDonate"), (4, "afterDonate"), (3, "beforeSwapReturnDelta"), (2, "afterSwapReturnDelta"),
         (1, "afterAddLiquidityReturnDelta"), (0, "afterRemoveLiquidityReturnDelta")]
RISK = {  # что право позволяет хуку и почему это важно для LP
    "afterRemoveLiquidityReturnDelta": "может удержать часть средств при выводе ликвидности",
    "afterAddLiquidityReturnDelta": "может изменить сумму при внесении ликвидности",
    "beforeRemoveLiquidity": "может заблокировать или ограничить вывод ликвидности",
    "beforeSwapReturnDelta": "может сам исполнять свопы и менять суммы (своя кривая, своя комиссия)",
    "afterSwapReturnDelta": "может брать часть результата свопа",
}
OK = {"stable", "eth", "btc"}


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def hook_flags(addr: str) -> list[str]:
    v = int(addr, 16) & 0x3FFF
    return [n for b, n in FLAGS if v >> b & 1]


def contract_name(chain: str, addr: str) -> dict:
    base = EXPLORER.get(chain)
    if not base:
        return {}
    try:
        j = get_json(f"{base}/api/v2/addresses/{addr}", cache_ttl=7 * 86400, min_interval=0.3, retries=2)
    except RuntimeError:
        return {}
    impl = (j.get("implementations") or [{}])[0].get("name") if j.get("implementations") else None
    return {"name": impl or j.get("name"), "verified": j.get("is_verified"),
            "proxy": bool(j.get("implementations")), "creator": j.get("creator_address_hash")}


def position_apr(p: dict, H: dict, width: float, usd: float = 10_000) -> tuple | None:
    """Fee APR позиции ±width при текущей активной ликвидности (по комиссиям за 7 и 30 дней)."""
    try:
        tk = get_json(f"{KRYSTAL_BASE}/v1/pools/{p['_cid']}/{p['poolAddress']}/ticks", headers=H,
                      cache_ttl=6 * 3600, params={"factoryAddress": p["protocol"]["factoryAddress"]})
    except RuntimeError:
        return None
    if not isinstance(tk, list) or not tk:
        return None
    _, Lraw = active_liquidity(tk, p["currentSqrtPriceX96"])
    if not Lraw:
        return None
    L = human_L(Lraw, p["token0"]["token"]["decimals"], p["token1"]["token"]["decimals"])
    P, pr1 = p["poolPrice"], p["token1"].get("price") or 1.0
    Lpos = usd / pr1 / value_per_L(P, P / (1 + width), P * (1 + width))
    share = Lpos / (L + Lpos)
    f7 = (p.get("stats7d") or {}).get("fee") or 0
    f30 = (p.get("stats30d") or {}).get("fee") or 0
    return f7 / 7 * share * 365 / usd * 100, f30 / 30 * share * 365 / usd * 100


def main():
    H = krystal_headers()
    pools = []
    for cid, cname in CHAINS.items():
        for pr in ["uniswapv4"] + (["pancakev4"] if cid in (56, 8453) else []):
            off = 0
            while True:
                d = get_json(f"{KRYSTAL_BASE}/v1/pools", headers=H, cache_ttl=3 * 3600, params=dict(
                    chainId=cid, protocol=pr, sortBy=1, minTvl=10000, limit=1000, offset=off,
                    includeTokenPrice="true"))
                if not isinstance(d, list) or not d:
                    break
                for p in d:
                    p["_cid"], p["_chain"] = cid, cname
                pools += d
                if len(d) < 1000:
                    break
                off += 1000
    log(f"пулов v4/Infinity с TVL ≥ $10k: {len(pools)}")

    def is_major(p):
        return {token_class(p["token0"]["token"]["symbol"]), token_class(p["token1"]["token"]["symbol"])} <= OK

    groups = defaultdict(list)
    for p in pools:
        h = (p.get("hook") or "0x0").lower()
        if int(h, 16):
            groups[(p["_chain"], p["protocol"]["key"], h)].append(p)
    hooks = []
    for (chain, proto, h), ps in groups.items():
        tvl = sum(p["tvl"] for p in ps)
        vol7 = sum((p.get("stats7d") or {}).get("volume") or 0 for p in ps)
        fee7 = sum((p.get("stats7d") or {}).get("fee") or 0 for p in ps)
        fl = hook_flags(h) if proto == "uniswapv4" else []
        hooks.append({
            "chain": chain, "protocol": proto, "hook": h, "pools": len(ps), "tvl": tvl, "vol_7d": vol7, "fee_7d": fee7,
            "fee_apr_7d": fee7 / 7 * 365 / tvl * 100 if tvl else 0,
            "majors_tvl": sum(p["tvl"] for p in ps if is_major(p)),
            "flags": fl, "risk_flags": [f for f in fl if f in RISK],
            "top_pairs": [f"{p['token0']['token']['symbol']}/{p['token1']['token']['symbol']} {p['feeTier'] / 1e4:g}%"
                          for p in sorted(ps, key=lambda p: -p["tvl"])[:3]],
        })
    hooks.sort(key=lambda r: -r["tvl"])
    # названия: для хуков с мейджорами и топ-40 по TVL
    for r in hooks:
        if r["majors_tvl"] > 20_000 or hooks.index(r) < 40:
            r.update(contract_name(r["chain"], r["hook"]))
    # хуковые пулы мейджоров против лучшего пула без хука той же пары
    majors = []
    best_plain, best_plain_exact = {}, {}

    def norm(sym):  # ETH и WETH считаем одним токеном
        s = sym.upper()
        return "ETH" if s in ("ETH", "WETH") else s

    for p in pools:
        if not is_major(p) or p["tvl"] < 50_000 or int((p.get("hook") or "0x0"), 16):
            continue
        pair = tuple(sorted([token_class(p["token0"]["token"]["symbol"]), token_class(p["token1"]["token"]["symbol"])]))
        exact = tuple(sorted([norm(p["token0"]["token"]["symbol"]), norm(p["token1"]["token"]["symbol"])]))
        for d, key in ((best_plain, (p["_chain"], pair)), (best_plain_exact, (p["_chain"], exact))):
            if key not in d or p["tvl"] > d[key]["tvl"]:
                d[key] = p
    names = {(r["chain"], r["hook"]): r.get("name") for r in hooks}
    for p in pools:
        h = (p.get("hook") or "0x0").lower()
        if not int(h, 16) or not is_major(p) or p["tvl"] < 50_000:
            continue
        pair = tuple(sorted([token_class(p["token0"]["token"]["symbol"]), token_class(p["token1"]["token"]["symbol"])]))
        width = 0.001 if pair in (("stable", "stable"), ("btc", "btc"), ("eth", "eth")) else 0.20
        a = position_apr(p, H, width)
        exact = tuple(sorted([norm(p["token0"]["token"]["symbol"]), norm(p["token1"]["token"]["symbol"])]))
        plain = best_plain_exact.get((p["_chain"], exact)) or best_plain.get((p["_chain"], pair))
        b = position_apr(plain, H, width) if plain else None
        majors.append({
            "chain": p["_chain"], "pair": f"{p['token0']['token']['symbol']}/{p['token1']['token']['symbol']}",
            "fee_pct": p["feeTier"] / 1e4, "hook": h, "hook_name": names.get((p["_chain"], h)),
            "flags": hook_flags(h) if p["protocol"]["key"] == "uniswapv4" else [], "tvl": p["tvl"],
            "width_pct": width * 100,
            "hook_pos_apr_7d": a[0] if a else None, "hook_pos_apr_30d": a[1] if a else None,
            "plain_pool": (f"{plain['token0']['token']['symbol']}/{plain['token1']['token']['symbol']} "
                           f"{plain['feeTier'] / 1e4:g}% (${plain['tvl'] / 1e6:.1f}M)") if plain else None,
            "plain_pos_apr_7d": b[0] if b else None, "plain_pos_apr_30d": b[1] if b else None,
        })
    majors.sort(key=lambda r: -r["tvl"])
    save(PROC / "hooks.json", {"generated_at": int(time.time()), "hooks": hooks, "majors": majors})
    _csv(PROC / "hooks.csv", hooks)
    _csv(PROC / "hooks_majors.csv", majors)

    tot = sum(r["tvl"] for r in hooks)
    maj = sum(r["majors_tvl"] for r in hooks)
    print(f"\nХуков: {len(hooks)}, TVL их пулов ${tot / 1e6:,.1f}M, из них в парах ETH/BTC/стейблы ${maj / 1e6:,.1f}M")
    print(f"С рискованными правами (меняют суммы свопа или вывода, могут блокировать вывод): "
          f"{sum(1 for r in hooks if r['risk_flags'])} хуков, TVL ${sum(r['tvl'] for r in hooks if r['risk_flags']) / 1e6:,.1f}M")
    print("\nХуковые пулы ETH/BTC/стейблы против лучшего пула без хука (доход позиции, % годовых по 7д/30д):")
    for r in majors[:25]:
        def f(a, b):
            return "—" if a is None else f"{a:5.1f}/{b:5.1f}"
        print(f"  {r['chain']:9s} {r['pair']:12s} {r['fee_pct']:<7g} {str(r['hook_name'] or r['hook'][:10])[:24]:24s}"
              f" TVL ${r['tvl'] / 1e6:6.2f}M ±{r['width_pct']:g}%: хук {f(r['hook_pos_apr_7d'], r['hook_pos_apr_30d'])}"
              f" | без хука {r['plain_pool']}: {f(r['plain_pos_apr_7d'], r['plain_pos_apr_30d'])}")


if __name__ == "__main__":
    main()
