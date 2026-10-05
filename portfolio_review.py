#!/usr/bin/env python3
"""Разбор портфеля монет из CSV: текущая стоимость, минус/плюс, во сколько раз монета должна вырасти
до безубыточности, динамика цены, доступный стейкинг и цена ETH, при которой портфель выходит в ноль.

CSV (с заголовком): ticker,invested,qty   — пример в portfolio.example.csv
Запуск:  python3 portfolio_review.py --csv data/private/portfolio.csv [--core ETH]
Результат: консоль + data/private/portfolio_review.json (личные данные в git не попадают).
"""
from __future__ import annotations

import argparse
import csv
import time

from lpscan.common import LLAMA_YIELDS, ROOT, get_json, save

COINGECKO = {
    "BTC": "bitcoin", "ETH": "ethereum", "STRK": "starknet", "AVAX": "avalanche-2", "ATOM": "cosmos",
    "MATIC": "polygon-ecosystem-token", "POL": "polygon-ecosystem-token",  # MATIC мигрировал в POL 1:1
    "APT": "aptos", "ARB": "arbitrum", "SUI": "sui", "ZK": "zksync", "BSW": "biswap", "PORTAL": "portal-2",
    "SEI": "sei-network", "1INCH": "1inch", "SOL": "solana", "OP": "optimism", "LINK": "chainlink",
    "DOT": "polkadot", "TIA": "celestia", "NEAR": "near", "TON": "the-open-network",
}
# стейкинг через ликвидные токены (DefiLlama yields): тикер → (проект, символ пула)
STAKING = {
    "ETH": ("lido", "STETH"), "AVAX": ("benqi-staked-avax", "SAVAX"), "STRK": ("endur", "STRK"),
    "MATIC": ("stader", "MATICX"), "POL": ("stader", "MATICX"), "SUI": ("current", "HASUI"),
    "APT": ("amnis-finance", "APT"),
}
PRIVATE = ROOT / "data" / "private"


def prices(tickers: list[str]) -> dict:
    keys = ",".join(sorted({f"coingecko:{COINGECKO[t]}" for t in tickers if t in COINGECKO}))
    now = get_json(f"https://coins.llama.fi/prices/current/{keys}", cache_ttl=600)["coins"]
    t = int(time.time()) // 3600 * 3600
    past = {d: get_json(f"https://coins.llama.fi/prices/historical/{t - d * 86400}/{keys}", cache_ttl=3600,
                        params={"searchWidth": "12h"}).get("coins", {}) for d in (30, 90, 365)}
    out = {}
    for tk in tickers:
        c = f"coingecko:{COINGECKO.get(tk, '?')}"
        p = now.get(c, {}).get("price")
        out[tk] = {"price": p, **{f"chg_{d}d": (p / past[d][c]["price"] - 1) * 100
                                  if p and past[d].get(c, {}).get("price") else None for d in past}}
    return out


def staking_yields() -> dict:
    pools = get_json(f"{LLAMA_YIELDS}/pools")["data"]
    out = {}
    for tk, (proj, sym) in STAKING.items():
        c = [p for p in pools if p["project"] == proj and p["symbol"].upper() == sym]
        if c:
            p = max(c, key=lambda p: p["tvlUsd"])
            out[tk] = p.get("apyMean30d") or p.get("apy")
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", required=True)
    ap.add_argument("--core", default="ETH", help="монета, в которую считается сценарий ротации")
    a = ap.parse_args()
    rows = []
    with open(a.csv) as f:
        for r in csv.DictReader(f):
            if not r.get("qty"):
                continue
            rows.append({"ticker": r["ticker"].strip().upper(), "invested": float(r["invested"] or 0),
                         "qty": float(str(r["qty"]).replace(",", "."))})
    px = prices([r["ticker"] for r in rows] + [a.core])
    st = staking_yields()
    for r in rows:
        p = px[r["ticker"]]["price"] or 0
        r.update(px[r["ticker"]], value=r["qty"] * p, avg=r["invested"] / r["qty"] if r["qty"] else None,
                 staking_apy=st.get(r["ticker"]))
        r["pnl"] = r["value"] - r["invested"]
        r["pnl_pct"] = r["pnl"] / r["invested"] * 100 if r["invested"] else None
        r["x_to_breakeven"] = r["invested"] / r["value"] if r["value"] else None
    inv = sum(r["invested"] for r in rows)
    val = sum(r["value"] for r in rows)
    core_px = px[a.core]["price"]
    core_qty = sum(r["qty"] for r in rows if r["ticker"] == a.core)
    others = val - core_qty * core_px
    summary = {
        "invested": inv, "value": val, "pnl": val - inv, "pnl_pct": (val / inv - 1) * 100,
        "core": a.core, "core_price": core_px, "core_share": core_qty * core_px / val * 100,
        # альты остаются как есть: какая нужна цена core-монеты
        "breakeven_core_if_hold": (inv - others) / core_qty if core_qty else None,
        # всё переложено в core-монету по текущим ценам
        "breakeven_core_if_rotated": inv / (val / core_px),
        "years_to_breakeven_at_yield": {y: None for y in (5, 10, 20)},
    }
    for y in (5, 10, 20):
        import math
        summary["years_to_breakeven_at_yield"][y] = math.log(inv / val) / math.log(1 + y / 100)
    PRIVATE.mkdir(parents=True, exist_ok=True)
    save(PRIVATE / "portfolio_review.json", {"rows": rows, "summary": summary})
    print(f"{'тикер':7s} {'вложено':>9s} {'кол-во':>11s} {'ср.цена':>9s} {'цена':>10s} {'стоимость':>10s}"
          f" {'результат':>10s} {'%':>6s} {'×до нуля':>8s} {'90д':>6s} {'1г':>6s} {'стейкинг':>8s}")
    for r in sorted(rows, key=lambda r: -r["value"]):
        def f(x, n=0, s=""):
            return "—" if x is None else f"{x:,.{n}f}{s}"
        print(f"{r['ticker']:7s} {f(r['invested']):>9s} {f(r['qty'], 2):>11s} {f(r['avg'], 3):>9s} {f(r['price'], 4):>10s}"
              f" {f(r['value']):>10s} {f(r['pnl']):>10s} {f(r['pnl_pct'], 0, '%'):>6s} {f(r['x_to_breakeven'], 1, '×'):>8s}"
              f" {f(r['chg_90d'], 0, '%'):>6s} {f(r['chg_365d'], 0, '%'):>6s} {f(r['staking_apy'], 1, '%'):>8s}")
    s = summary
    print(f"\nИтого: вложено ${s['invested']:,.0f}, сейчас ${s['value']:,.0f}, результат ${s['pnl']:,.0f} ({s['pnl_pct']:.0f}%)")
    print(f"{s['core']}: {s['core_share']:.0f}% портфеля. Безубыточность по цене {s['core']}: если альты оставить — "
          f"${s['breakeven_core_if_hold']:,.0f}; если всё переложить в {s['core']} — ${s['breakeven_core_if_rotated']:,.0f}")
    print("Лет до безубыточности только за счёт доходности (без роста цен): " +
          ", ".join(f"{y}%/год → {v:.1f} лет" for y, v in s["years_to_breakeven_at_yield"].items()))


if __name__ == "__main__":
    main()
