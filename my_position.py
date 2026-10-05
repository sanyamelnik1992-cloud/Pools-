#!/usr/bin/env python3
"""Разбор LP-позиций и кошелька: что с позицией сейчас и стоит ли её пересобирать.

Запуск:  python3 my_position.py --wallet 0x... [--chain 42161]
Результат печатается в консоль и сохраняется в data/private/ (папка в .gitignore —
личные данные кошелька в репозиторий не попадают).

Что считается по каждой открытой позиции:
  * состав, комиссии (собранные и несобранные), PnL и сравнение с холдом — из Krystal;
  * текущая доходность от комиссий: доля позиции в активной ликвидности пула × комиссии пула
    за 7 и 30 дней;
  * сценарии: стоимость позиции при разных ценах против холда текущего состава;
  * альтернативы пересборки: диапазоны вокруг текущей цены — ожидаемая fee APR сейчас и
    бэктест на трёх 30-дневных окнах (позиция открывается заново в начале окна).
Кошелёк: нативный баланс по RPC (Krystal его не показывает), токены из Krystal, спам отсеивается.
"""
from __future__ import annotations

import argparse
import math
import statistics as st
import time

from collect import active_liquidity
from lpscan.common import (CHAINS, KRYSTAL_BASE, ROOT, get_json, krystal_headers, rpc_call, save)
from lpscan.metrics import amounts_for_L, backtest_cl, human_L, slice_hist, value_per_L

RPCS = {1: "https://ethereum-rpc.publicnode.com", 42161: CHAINS[42161]["rpc"], 8453: CHAINS[8453]["rpc"]}
SPAM_HINTS = ("claim", "visit", "http", "www.", ".com", ".xyz", ".io", "t.me", "airdrop", "voucher",
              "reward", "access", "distribution", "invite", "|", "[")
PRIVATE = ROOT / "data" / "private"


def tok_amount(a):
    return int(a["balance"]) / 10 ** a["token"]["decimals"]


def wallet_overview(wallet: str) -> dict:
    H = krystal_headers()
    native = {}
    for cid, rpc in RPCS.items():
        try:
            native[cid] = int(rpc_call(rpc, "eth_getBalance", [wallet, "latest"]), 16) / 1e18
        except RuntimeError:
            native[cid] = None
    bal = get_json(f"{KRYSTAL_BASE}/v1/balances/{wallet}", headers=H, cache_ttl=600,
                   params={"includeDustToken": "true"})
    tokens, spam = [], 0
    for ch in bal:
        for b in ch["balances"]:
            t = b["token"]
            text = f'{t.get("symbol", "")} {t.get("name", "")}'.lower()
            if any(h in text for h in SPAM_HINTS) or b.get("price") is None:
                spam += 1
                continue
            tokens.append({"chain": ch["chain"]["name"], "symbol": t["symbol"],
                           "amount": int(b["balance"]) / 10 ** t["decimals"], "price": b.get("price"),
                           "value": b.get("value")})
    eth = get_json("https://coins.llama.fi/prices/current/coingecko:ethereum", cache_ttl=600)
    eth_px = eth["coins"]["coingecko:ethereum"]["price"]
    return {"native_eth": native, "eth_price": eth_px, "tokens": tokens, "spam_tokens": spam}


def position_report(p: dict, chain_id: int) -> dict:
    H = krystal_headers()
    pool = p["pool"]["poolAddress"].lower()
    factory = p["pool"]["protocol"]["factoryAddress"]
    cur = p["currentAmounts"]
    t0, t1 = cur[0]["token"], cur[1]["token"]
    d0, d1 = t0["decimals"], t1["decimals"]
    P = p["pool"]["poolPrice"]                       # token1 за token0
    pa, pb = p["minPrice"], p["maxPrice"]
    L_pos = human_L(int(p["liquidity"]), d0, d1)
    fees_pending = sum(a.get("value") or 0 for a in p["tradingFee"]["pending"])
    fees_claimed = sum(a.get("value") or 0 for a in p["tradingFee"]["claimed"])
    value = p["currentPositionValue"] - fees_pending if p["currentPositionValue"] else 0
    lp_value = sum(a.get("value") or 0 for a in cur)
    # пул: комиссии и активная ликвидность
    prow = get_json(f"{KRYSTAL_BASE}/v1/pools/{chain_id}/{pool}", headers=H, cache_ttl=1800,
                    params={"factoryAddress": factory})
    ticks = get_json(f"{KRYSTAL_BASE}/v1/pools/{chain_id}/{pool}/ticks", headers=H, cache_ttl=1800,
                     params={"factoryAddress": factory})
    _, L_act_raw = active_liquidity(ticks, prow["currentSqrtPriceX96"])
    L_act = human_L(L_act_raw, d0, d1)
    fee7 = prow["stats7d"]["fee"] / 7
    fee30 = prow["stats30d"]["fee"] / 30
    in_range = pa <= P <= pb
    p1_usd = cur[1].get("price") or 1.0

    def fee_apr_for(L, v):
        share = L / (L_act + L)
        return {"fee_apr_7d": fee7 * share * 365 / v * 100, "fee_apr_30d": fee30 * share * 365 / v * 100}

    now_apr = fee_apr_for(L_pos, lp_value) if in_range and lp_value > 0 else {"fee_apr_7d": 0, "fee_apr_30d": 0}
    # сценарии по цене (в единицах token1 за token0)
    a0_now, a1_now = amounts_for_L(L_pos, P, pa, pb)
    scen = []
    for k in (0.65, 0.75, 0.85, 0.95, 1.0, 1.05, 1.1, 1.2, 1.35, 1.5):
        Px = P * k
        a0, a1 = amounts_for_L(L_pos, Px, pa, pb)
        scen.append({"price": Px, "lp_token0": a0, "lp_token1": a1, "lp_value": (a0 * Px + a1) * p1_usd,
                     "hold_value": (a0_now * Px + a1_now) * p1_usd, "in_range": pa <= Px <= pb})
    # история пула (90 дней) для бэктеста альтернатив
    end = int(time.time()) // 3600 * 3600
    hist = get_json(f"{KRYSTAL_BASE}/v1/pools/{chain_id}/{pool}/historical", headers=H, cache_ttl=6 * 3600,
                    params=dict(startTime=end - 90 * 86400, endTime=end))
    hist = sorted(hist, key=lambda x: x["timestamp"])
    tvl_now = prow["tvl"]
    fee_pct = prow["feeTier"] / 1e4
    wins = [(hist[-1]["timestamp"] - (3 - i) * 30 * 86400, hist[-1]["timestamp"] - (2 - i) * 30 * 86400)
            for i in range(3)]
    v_new = lp_value + fees_pending
    alts = []
    for label, w, reb in [("Как сейчас (те же границы)", "abs", False), ("±10%, пересборка раз в месяц", 0.10, False),
                          ("±15%, пересборка раз в месяц", 0.15, False), ("±20%, пересборка раз в месяц", 0.20, False),
                          ("±30%, пересборка раз в месяц", 0.30, False), ("±10%, авто-ребаланс", 0.10, True),
                          ("±20%, авто-ребаланс", 0.20, True), ("Полный диапазон", None, False)]:
        if w == "abs":
            ra, rb = pa, pb
        elif w is None:
            ra, rb = P * 1e-6, P * 1e6
        else:
            ra, rb = P / (1 + w), P * (1 + w)
        L_alt = v_new / p1_usd / value_per_L(P, ra, rb)
        exp = fee_apr_for(L_alt, v_new) if ra <= P <= rb else {"fee_apr_7d": 0, "fee_apr_30d": 0}
        per = []
        for a, b in wins:
            res = backtest_cl(slice_hist(hist, a, b), L_act, tvl_now, None if w in ("abs", None) else w, fee_pct,
                              invert=False, rebalance=reb, range_abs=(pa, pb) if w == "abs" else None)
            if res:
                per.append(res)
        alts.append({"option": label, "range": [ra, rb], **exp,
                     "bt_vs_hold": [x["net_vs_hodl_apr"] for x in per],
                     "bt_usd": [x["net_usd_apr"] for x in per],
                     "bt_income": [x["fee_apr"] for x in per],
                     "bt_time_in_range": [x["time_in_range"] for x in per]})
    sigma = None
    rets = [math.log(b["poolPrice"] / a["poolPrice"]) for a, b in zip(hist[::24], hist[24::24])]
    if len(rets) > 10:
        sigma = st.pstdev(rets) * math.sqrt(365)
    return {
        "id": p["id"], "status": p["status"], "pair": f'{t0["symbol"]}/{t1["symbol"]}', "fee_tier": fee_pct,
        "opened": time.strftime("%Y-%m-%d", time.gmtime(p["openedTime"])), "price": P, "range": [pa, pb],
        "in_range": in_range, "lp_value": lp_value, "fees_pending": fees_pending, "fees_claimed": fees_claimed,
        "amounts": [tok_amount(cur[0]), tok_amount(cur[1])], "symbols": [t0["symbol"], t1["symbol"]],
        "at_lower": amounts_for_L(L_pos, pa * 0.999, pa, pb), "at_upper": amounts_for_L(L_pos, pb * 1.001, pa, pb),
        "performance": p.get("performance"), "fee_apr_now": now_apr, "pool_tvl": tvl_now,
        "pool_fee_7d_day": fee7, "share_of_active_liquidity": L_pos / (L_act + L_pos) * 100,
        "sigma_90d": sigma, "scenarios": scen, "alternatives": alts, "value_with_fees": v_new, "value": value,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--wallet", required=True)
    ap.add_argument("--chain", type=int, default=42161)
    a = ap.parse_args()
    H = krystal_headers()
    lst = get_json(f"{KRYSTAL_BASE}/v1/positions", headers=H, cache_ttl=600,
                   params={"wallet": a.wallet, "chainIds": a.chain, "limit": 100})
    out = {"wallet": a.wallet, "positions": [], "wallet_overview": wallet_overview(a.wallet)}
    for p in lst:
        if p.get("status") == "CLOSED":
            continue
        det = get_json(f"{KRYSTAL_BASE}/v1/positions/{a.chain}/{p['id']}", headers=H, cache_ttl=600,
                       params={"wallet": a.wallet})
        out["positions"].append(position_report(det, a.chain))
    PRIVATE.mkdir(parents=True, exist_ok=True)
    save(PRIVATE / f"positions_{a.wallet.lower()[:10]}.json", out)
    for r in out["positions"]:
        s0, s1 = r["symbols"]
        print(f"\n=== {r['pair']} {r['fee_tier']:g}% · {r['status']} · открыта {r['opened']} · "
              f"диапазон {r['range'][0]:.0f}–{r['range'][1]:.0f} · цена {r['price']:.0f}")
        print(f"  состав: {r['amounts'][0]:.4f} {s0} + {r['amounts'][1]:.2f} {s1} = ${r['lp_value']:,.0f}; "
              f"комиссии: несобранные ${r['fees_pending']:,.0f}, собранные ${r['fees_claimed']:,.0f}")
        print(f"  у нижней границы: {r['at_lower'][0]:.3f} {s0}; у верхней: {r['at_upper'][1]:,.0f} {s1}")
        print(f"  доля в активной ликвидности {r['share_of_active_liquidity']:.4f}% · fee APR сейчас: "
              f"{r['fee_apr_now']['fee_apr_7d']:.1f}% (по 7д), {r['fee_apr_now']['fee_apr_30d']:.1f}% (по 30д)")
        print("  performance:", r["performance"])
        for s in r["scenarios"]:
            print(f"   цена {s['price']:7.0f}: LP ${s['lp_value']:9,.0f} ({s['lp_token0']:.3f} {s0} + {s['lp_token1']:,.0f} {s1})"
                  f" | холд текущего состава ${s['hold_value']:9,.0f}")
        for x in r["alternatives"]:
            print(f"   {x['option']:32s} [{x['range'][0]:7.0f}–{x['range'][1]:9.0f}] fee APR сейчас {x['fee_apr_7d']:5.1f}%"
                  f" | бэктест к холду {'/'.join(f'{v:+.0f}' for v in x['bt_vs_hold'])}"
                  f" | в $ {'/'.join(f'{v:+.0f}' for v in x['bt_usd'])}"
                  f" | доход {'/'.join(f'{v:.0f}' for v in x['bt_income'])}")
    w = out["wallet_overview"]
    print("\n=== кошелёк: нативный ETH", w["native_eth"], "| цена ETH", round(w["eth_price"], 2))
    for t in w["tokens"]:
        print(f"  {t['chain']:10s} {t['symbol']:8s} {t['amount']:,.4f} ≈ ${t['value'] or 0:,.2f}")
    print(f"  спам-токенов скрыто: {w['spam_tokens']}")


if __name__ == "__main__":
    main()
