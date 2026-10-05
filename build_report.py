#!/usr/bin/env python3
"""Сборка HTML-отчёта из data/processed/report_data.json → report/index.html.

Таблицы и графики строятся из данных; текстовые выводы ссылаются на рассчитанные числа.
"""
from __future__ import annotations

import html
import json
import statistics as st
import time
from collections import defaultdict
from pathlib import Path

from lpscan.common import PROC, ROOT, load

OUT = ROOT / "report" / "index.html"


# ------------------------------------------------------------------ форматирование
def esc(s):
    return html.escape(str(s))


def usd(x, d=1):
    if x is None:
        return "—"
    a = abs(x)
    if a >= 1e9:
        return f"${x / 1e9:.{d}f} млрд"
    if a >= 1e6:
        return f"${x / 1e6:.{d}f} млн"
    if a >= 1e3:
        return f"${x / 1e3:.0f} тыс"
    return f"${x:.0f}"


def pct(x, d=1, sign=False):
    if x is None:
        return "—"
    s = f"{x:+.{d}f}" if sign else f"{x:.{d}f}"
    return s.replace("-", "−") + "%"


def num(x, d=2):
    return "—" if x is None else f"{x:.{d}f}".replace("-", "−")


def fee_tier(x):
    return f"{x:.4g}%"


def risk_chip(r):
    cls = "ok" if r <= 3 else "mid" if r <= 5 else "hi"
    return f'<span class="chip {cls}" title="Риск-балл 1–10">{r}</span>'


def r_chip(r):
    if r is None:
        return "—"
    cls = "ok" if r >= 1 else "mid" if r >= 0.5 else "hi"
    return f'<span class="chip {cls}">{r:.2f}</span>'


def flags_html(fl):
    if not fl:
        return '<span class="muted">нет</span>'
    return " ".join(f'<span class="flag">{esc(f)}</span>' for f in fl)


def table(cols, rows, cls="", sortable=True):
    """cols: [(заголовок, тип)] где тип: 'txt' | 'num'; rows: список списков (html, sort_value)."""
    th = "".join(f'<th class="{t}" scope="col">{esc(h)}</th>' for h, t in cols)
    body = []
    for row in rows:
        tds = []
        for (h, t), cell in zip(cols, row):
            if isinstance(cell, tuple):
                v, sv = cell
                tds.append(f'<td class="{t}" data-v="{sv if sv is not None else ""}">{v}</td>')
            else:
                tds.append(f'<td class="{t}">{cell}</td>')
        body.append("<tr>" + "".join(tds) + "</tr>")
    s = " sortable" if sortable else ""
    return (f'<div class="tw"><table class="{cls}{s}"><thead><tr>{th}</tr></thead>'
            f'<tbody>{"".join(body)}</tbody></table></div>')


# ------------------------------------------------------------------ данные
def main():
    d = load(PROC / "report_data.json")
    P = d["pools"]
    meta = d["meta"]
    asof = time.strftime("%d.%m.%Y", time.gmtime(meta["collected_at"]))
    byk = {p["key"]: p for p in P}
    ok = [p for p in P if not p["red_flags"]]

    def find(chain, dex, pair, fee=None):
        c = [p for p in P if p["chain"] == chain and p["dex"] == dex and p["pair"] == pair
             and (fee is None or abs(p["fee_pct"] - fee) < 1e-9)]
        return max(c, key=lambda p: p["tvl"]) if c else None

    eth = find("Arbitrum", "Uniswap v3", "WETH/USDC", 0.05)
    chains = d["chains"]

    # ---------------- Таблица: топ по устойчивой fee APR
    def top_fee_rows(chain, n=15):
        rows = sorted([p for p in ok if p["chain"] == chain and p["tvl"] >= 1e6],
                      key=lambda p: -p["fee_apr_sustained"])[:n]
        out = []
        for p in rows:
            out.append([
                f'<b>{esc(p["pair"])}</b><div class="sub">{esc(p["category"])}</div>',
                f'{esc(p["dex"])}<div class="sub">комиссия {fee_tier(p["fee_pct"])}</div>',
                (usd(p["tvl"]), p["tvl"]),
                (pct(p["fee_apr_7d"]), p["fee_apr_7d"]),
                (pct(p["fee_apr_30d"]), p["fee_apr_30d"]),
                (f'<b>{pct(p["fee_apr_sustained"])}</b>', p["fee_apr_sustained"]),
                (pct(p["reward_apr_staked"] or p["reward_apr"]) if p["reward_apr"] else "—", p["reward_apr"]),
                (num(p["vol_tvl_7d_daily"]), p["vol_tvl_7d_daily"]),
                (pct((p["sigma"] or 0) * 100, 0) if p["sigma"] is not None else "—", p["sigma"]),
                (r_chip(p["fee_lvr_ratio"]), p["fee_lvr_ratio"]),
                (risk_chip(p["risk"]), p["risk"]),
            ])
        return out

    top_cols = [("Пара", "txt"), ("DEX", "txt"), ("TVL", "num"), ("Fee APR 7д", "num"),
                ("Fee APR 30д", "num"), ("Устойчивая", "num"), ("Награды", "num"),
                ("Объём/TVL в день", "num"), ("Волат. σ", "num"), ("R = fee/IL", "num"), ("Риск", "num")]

    # ---------------- Эмиссия Aerodrome
    aero_rows = []
    for p in sorted([p for p in P if p["reward_apr"] > 0 and p["tvl"] >= 1e6 and p["dex"].startswith("Aerodrome")],
                    key=lambda p: -p["reward_usd_day"])[:18]:
        aero_rows.append([
            f'<b>{esc(p["pair"])}</b><div class="sub">{esc(p["category"])}</div>',
            (fee_tier(p["fee_pct"]), p["fee_pct"]),
            (usd(p["tvl"]), p["tvl"]),
            (usd(p["reward_usd_day"] * 365), p["reward_usd_day"]),
            (pct(p["fee_apr_30d"]), p["fee_apr_30d"]),
            (f'<b>{pct(p["reward_apr_staked"] or p["reward_apr"])}</b>', p["reward_apr_staked"] or p["reward_apr"]),
            (pct((p["staked_share"] or 0) * 100, 0) if p["staked_share"] else "—", p["staked_share"]),
            (pct((p["sigma"] or 0) * 100, 0) if p["sigma"] is not None else "—", p["sigma"]),
            flags_html(p["red_flags"]),
        ])
    aero_cols = [("Пара", "txt"), ("Комиссия", "num"), ("TVL", "num"), ("Эмиссия AERO в год", "num"),
                 ("Fee APR 30д (без стейка)", "num"), ("APR в AERO (стейк)", "num"), ("Доля в стейке", "num"),
                 ("Волат. σ", "num"), ("Флаги", "txt")]

    # ---------------- DEX
    dex_rows = []
    for a in sorted(d["by_dex"], key=lambda a: -a["fee_7d"]):
        if a["tvl"] < 2e5:
            continue
        dex_rows.append([
            esc(a["chain"]), f'<b>{esc(a["dex"])}</b>', (a["n"], a["n"]), (usd(a["tvl"]), a["tvl"]),
            (usd(a["fee_7d"] / 7 * 365), a["fee_7d"]),
            (pct(a["w_fee_apr"]), a["w_fee_apr"]),
            (pct(a["median_apr_big"]) if a["median_apr_big"] is not None else "—", a["median_apr_big"]),
            (pct(a["w_reward_apr"]) if a["w_reward_apr"] > 0.05 else "—", a["w_reward_apr"]),
            esc(", ".join(a["top_pairs"]) or "—"),
        ])
    dex_cols = [("Сеть", "txt"), ("DEX", "txt"), ("Пулов", "num"), ("TVL", "num"), ("Комиссии, год (по 7д)", "num"),
                ("Fee APR (взвеш.)", "num"), ("Медиана пулов >$1 млн", "num"), ("Эмиссия APR", "num"),
                ("Лучшие пары (TVL>$0.5 млн)", "txt")]

    # ---------------- Категории
    cat_rows = []
    order = ["Стейбл/стейбл", "Коррелир. (ETH/LST, BTC/BTC)", "ETH/BTC", "Голубая фишка/стейбл", "Крупный альт",
             "Токенизир. акции", "Альт/мем (long-tail)"]
    for a in sorted(d["by_cat"], key=lambda a: (order.index(a["category"]) if a["category"] in order else 9,
                                                a["chain"])):
        cat_rows.append([
            f'<b>{esc(a["category"])}</b>', esc(a["chain"]), (usd(a["tvl"]), a["tvl"]),
            (pct(a["w_fee_apr"]), a["w_fee_apr"]),
            (pct(a["w_reward_apr"]) if a["w_reward_apr"] > 0.05 else "—", a["w_reward_apr"]),
            (pct((a["median_sigma"] or 0) * 100, 0) if a["median_sigma"] is not None else "—", a["median_sigma"]),
            (pct((a["median_sigma"] or 0) ** 2 / 8 * 100, 1) if a["median_sigma"] is not None else "—",
             a["median_sigma"]),
            (r_chip(a["median_fee_lvr"]), a["median_fee_lvr"]),
        ])
    cat_cols = [("Тип пары", "txt"), ("Сеть", "txt"), ("TVL", "num"), ("Fee APR (взвеш.)", "num"),
                ("Эмиссия APR", "num"), ("Медиана σ", "num"), ("IL-порог σ²/8 (полный диапазон)", "num"),
                ("Медиана R", "num")]

    # ---------------- Подозрительные APR
    sus = sorted([p for p in P if p["red_flags"] and p["tvl"] >= 50e3 and
                  (p["fee_apr_24h"] > 60 or p["reward_apr"] > 60)],
                 key=lambda p: -(p["fee_apr_24h"] + p["reward_apr"]))[:22]
    sus_rows = [[f'<b>{esc(p["pair"])}</b><div class="sub">{esc(p["chain"])} · {esc(p["dex"])}</div>',
                 (usd(p["tvl"]), p["tvl"]), (pct(p["fee_apr_24h"], 0), p["fee_apr_24h"]),
                 (pct(p["fee_apr_30d"], 0), p["fee_apr_30d"]),
                 (pct(p["reward_apr"], 0) if p["reward_apr"] else "—", p["reward_apr"]),
                 (num(p["vol_tvl_24h"], 1), p["vol_tvl_24h"]), flags_html(p["red_flags"])] for p in sus]
    sus_cols = [("Пара", "txt"), ("TVL", "num"), ("Fee APR 24ч", "num"), ("Fee APR 30д", "num"),
                ("Награды APR", "num"), ("Объём/TVL 24ч", "num"), ("Почему подозрительно", "txt")]

    # ---------------- Сверка
    xc_rows = []
    for x in d["xcheck"][:16]:
        def dev(a, b):
            if not a or not b:
                return "—"
            v = (a / b - 1) * 100
            cls = "ok" if abs(v) < 5 else "mid" if abs(v) < 20 else "hi"
            return f'<span class="chip {cls}">{v:+.0f}%</span>'.replace("-", "−")
        p = byk.get(next((k for k, v in byk.items() if v["pair"] == x["pair"] and v["chain"] == x["chain"]
                          and v["dex"] == x["dex"] and abs(v["fee_pct"] - x["fee_pct"]) < 1e-9), None))
        flag = flags_html(p["red_flags"]) if p else ""
        xc_rows.append([f'<b>{esc(x["pair"])}</b> <span class="sub">{fee_tier(x["fee_pct"])}</span>'
                        f'<div class="sub">{esc(x["chain"])} · {esc(x["dex"])}</div>',
                        (usd(x["tvl"]), x["tvl"]), dev(x["tvl"], x["gecko_tvl"]), dev(x["tvl"], x["dexs_tvl"]),
                        dev(x["tvl"], x["llama_tvl"]), dev(x["tvl"], x["rpc_usd"]),
                        dev(x["vol_24h"], x["gecko_vol_24h"]),
                        (pct(x["fee_apr_7d"]), x["fee_apr_7d"]),
                        (pct(x["llama_apy_base"]) if x["llama_apy_base"] is not None else "—", x["llama_apy_base"]),
                        flag])
    xc_cols = [("Пул", "txt"), ("TVL Krystal", "num"), ("Δ GeckoTerminal", "txt"), ("Δ DexScreener", "txt"),
               ("Δ DefiLlama", "txt"), ("Δ ончейн (RPC)", "txt"), ("Δ объём 24ч (Gecko)", "txt"),
               ("Fee APR 7д (Krystal)", "num"), ("APY base (Llama)", "num"), ("Флаги", "txt")]

    # ---------------- DefiLlama: то, чего нет в Krystal
    ll = [p for p in d["llama_dex"] if p["project"] in ("aerodrome-v1", "camelot-v2", "camelot-v3", "curve-dex",
                                                        "fluid-dex", "balancer-v3", "balancer-v2", "sushiswap")
          and p["tvlUsd"] >= 1.5e6 and (p["apyBase"] or 0) + (p["apyReward"] or 0) < 400]
    ll_rows = [[f'<b>{esc(p["symbol"])}</b>', esc(p["chain"]), esc(p["project"]), (usd(p["tvlUsd"]), p["tvlUsd"]),
                (pct(p["apyBase"]) if p["apyBase"] is not None else "—", p["apyBase"]),
                (pct(p["apyReward"]) if p["apyReward"] else "—", p["apyReward"]),
                (pct(p["apyMean30d"]) if p["apyMean30d"] is not None else "—", p["apyMean30d"]),
                "да" if p["ilRisk"] == "yes" else "нет"]
               for p in sorted(ll, key=lambda p: -p["tvlUsd"])[:16]]
    ll_cols = [("Пул", "txt"), ("Сеть", "txt"), ("Протокол", "txt"), ("TVL", "num"), ("APY комиссии", "num"),
               ("APY награды", "num"), ("Средн. APY 30д", "num"), ("Риск IL", "txt")]

    # ---------------- Бэктесты
    bt = d["backtests"]
    bt_groups = defaultdict(list)
    for b in bt:
        bt_groups[(b["key"], b["window"])].append(b)
    bt_rows = []
    seen = []
    for (key, w), rows in bt_groups.items():
        if key not in seen:
            seen.append(key)
    for key in seen:
        p = byk[key]
        cells = [f'<b>{esc(p["pair"])}</b> <span class="sub">{fee_tier(p["fee_pct"])}</span>'
                 f'<div class="sub">{esc(p["chain"])} · {esc(p["dex"])}</div>']
        for w in (90, 30):
            rows = bt_groups.get((key, w), [])
            if not rows:
                cells += ["—", "—"]
                continue
            full = next((b for b in rows if b["width_pct"] is None and b["mode"] == "Пассивно"), None)
            best = max(rows, key=lambda b: b["net_vs_hodl_apr"])
            wd = "полный" if best["width_pct"] is None else f'±{best["width_pct"]:g}%'
            cells.append((pct(full["net_vs_hodl_apr"], 1, True) if full else "—",
                          full["net_vs_hodl_apr"] if full else None))
            cells.append((f'<b>{pct(best["net_vs_hodl_apr"], 1, True)}</b>'
                          f'<div class="sub">{esc(best["mode"])}, {wd}; комиссии {pct(best["fee_apr"] + best["reward_apr"], 0)}</div>',
                          best["net_vs_hodl_apr"]))
        bt_rows.append(cells)
    bt_cols = [("Пул", "txt"), ("90д: полный диапазон", "num"), ("90д: лучшая настройка (задним числом)", "num"),
               ("30д: полный диапазон", "num"), ("30д: лучшая настройка (задним числом)", "num")]

    # ---------------- Blast
    blast = chains["Blast"]
    bl_rows = []
    for p in blast["pools"]:
        if p["reserve_usd"] > 50e6 and p["vol_24h"] < 1000:
            continue  # фейковая оценка TVL (пример: YES/WETH)
        bl_rows.append([f'<b>{esc(p["name"])}</b>', esc(p["dex"]), (usd(p["reserve_usd"]), p["reserve_usd"]),
                        (usd(p["vol_24h"]), p["vol_24h"]),
                        (pct(p["fee_apr_est"]) if p["fee_apr_est"] else "—", p["fee_apr_est"])])
    bl_rows = bl_rows[:8]
    bl_cols = [("Пул", "txt"), ("DEX", "txt"), ("TVL", "num"), ("Объём 24ч", "num"), ("Fee APR (оценка по 24ч)", "num")]

    # ---------------- Данные для графиков
    def chain_chart(ch):
        return [[t, round(v)] for t, v in chains[ch]["chart"][-90:]]

    scatter = []
    for p in P:
        if p["tvl"] < 1e6 or p["sigma"] is None or p["fee_apr_sustained"] <= 0.05:
            continue
        if "цена альта не подтверждена" in p["red_flags"] or "мёртвый пул: объёма нет" in p["red_flags"]:
            continue
        grp = 0 if p["category"] in ("Стейбл/стейбл", "Коррелир. (ETH/LST, BTC/BTC)") else \
            1 if p["category"] in ("ETH/BTC", "Голубая фишка/стейбл") else 2
        scatter.append({"x": round(p["sigma"] * 100, 1), "y": round(p["fee_apr_sustained"], 2), "g": grp,
                        "t": round(p["tvl"]), "n": f'{p["pair"]} {fee_tier(p["fee_pct"])} · {p["dex"]} · {p["chain"]}'})

    rbars = []
    for p in sorted([p for p in ok if p.get("fee_lvr_ratio") and p["tvl"] >= 2.5e6
                     and p["category"] not in ("Стейбл/стейбл", "Коррелир. (ETH/LST, BTC/BTC)")],
                    key=lambda p: -p["tvl"])[:16]:
        rbars.append({"n": f'{p["pair"]} {fee_tier(p["fee_pct"])} · {p["dex"]} · {p["chain"][:4]}',
                      "r": round(p["fee_lvr_ratio"], 2), "fee": round(p["fee_apr_fullrange"], 2),
                      "lvr": round(p["lvr_full_pct"], 2)})

    series_pick = []
    for spec in [("Arbitrum", "Uniswap v3", "WETH/USDC", 0.05), ("Base", "Uniswap v3", "WETH/USDC", 0.05),
                 ("Base", "Aerodrome Slipstream", "WETH/USDC", 0.0625), ("Arbitrum", "Uniswap v3", "WBTC/WETH", 0.05)]:
        p = find(*spec)
        if p and p["key"] in d["series"]:
            series_pick.append({"n": f'{p["pair"]} {fee_tier(p["fee_pct"])} · {p["dex"]} · {p["chain"]}',
                                "apr": d["series"][p["key"]]["apr"]})

    def bt_curve(chain, dex, pair, fee, window, modes):
        p = find(chain, dex, pair, fee)
        if not p:
            return None
        out = {"n": f'{pair} {fee_tier(fee)} · {dex} · {chain}', "series": []}
        for m in modes:
            rows = sorted([b for b in bt if b["key"] == p["key"] and b["window"] == window and b["mode"] == m
                           and b["width_pct"] is not None], key=lambda b: b["width_pct"])
            out["series"].append({"m": m, "pts": [[b["width_pct"], round(b["net_vs_hodl_apr"], 1),
                                                   round(b["fee_apr"] + b["reward_apr"], 1),
                                                   round(b["time_in_range"]), b["rebalances"]] for b in rows]})
        return out

    bt_eth = {w: bt_curve("Base", "Uniswap v3", "WETH/USDC", 0.3, w, ["Пассивно", "Авто-ребаланс"]) for w in (90, 30)}
    bt_aero = {w: bt_curve("Base", "Aerodrome Slipstream", "WETH/USDC", 0.0625, w,
                           ["Пассивно", "Стейк (AERO), пассивно", "Авто-ребаланс", "Стейк (AERO), авто-ребаланс"])
               for w in (90, 30)}

    payload = {"chains": {c: chain_chart(c) for c in ("Base", "Arbitrum", "Blast")},
               "scatter": scatter, "rbars": rbars, "series": series_pick, "btEth": bt_eth, "btAero": bt_aero}

    # ---------------- ключевые числа для текста
    def bt_get(chain, dex, pair, fee, window, mode, width):
        p = find(chain, dex, pair, fee)
        if not p:
            return None
        for b in bt:
            if b["key"] == p["key"] and b["window"] == window and b["mode"] == mode and b["width_pct"] == width:
                return b
        return None

    k = {
        "base_vol": chains["Base"]["vol_30d"], "arb_vol": chains["Arbitrum"]["vol_30d"],
        "blast_vol": chains["Blast"]["vol_30d"], "base_fees": chains["Base"]["fees_30d"],
        "arb_fees": chains["Arbitrum"]["fees_30d"], "blast_fees": chains["Blast"]["fees_30d"],
        "eth90": eth["price_chg_90d"] if eth else None, "eth30": eth["price_chg_30d"] if eth else None,
    }
    b_eth_full90 = bt_get("Base", "Uniswap v3", "WETH/USDC", 0.3, 90, "Пассивно", None)
    b_eth_auto1 = bt_get("Base", "Uniswap v3", "WETH/USDC", 0.3, 90, "Авто-ребаланс", 1.0)
    b_eth_auto10_30 = bt_get("Base", "Uniswap v3", "WETH/USDC", 0.3, 30, "Авто-ребаланс", 10.0)
    b_cbbtc5 = bt_get("Base", "Uniswap v3", "WETH/CBBTC", 0.05, 90, "Авто-ребаланс", 5.0)
    b_cbbtc5_30 = bt_get("Base", "Uniswap v3", "WETH/CBBTC", 0.05, 30, "Пассивно", 5.0)
    b_stab = bt_get("Arbitrum", "Uniswap v3", "USDC/USD₮0", 0.01, 90, "Пассивно", 0.05)
    b_aero_full = bt_get("Base", "Aerodrome Slipstream", "WETH/USDC", 0.0625, 90, "Стейк (AERO), пассивно", None)
    b_aero5 = bt_get("Base", "Aerodrome Slipstream", "WETH/USDC", 0.0625, 30, "Стейк (AERO), авто-ребаланс", 5.0)
    b_aero10 = bt_get("Base", "Aerodrome Slipstream", "WETH/USDC", 0.0625, 30, "Стейк (AERO), авто-ребаланс", 10.0)
    aero_weth = find("Base", "Aerodrome Slipstream", "WETH/USDC", 0.0625)
    msusd = find("Base", "Aerodrome Slipstream", "MSUSD/USDC")
    arb_stab = find("Arbitrum", "Uniswap v3", "USDC/USD₮0", 0.01)
    weth_cbbtc = find("Base", "Uniswap v3", "WETH/CBBTC", 0.05)
    base_eth_005 = find("Base", "Uniswap v3", "WETH/USDC", 0.05)
    n_flagged = sum(1 for p in P if p["red_flags"])
    n_high = sum(1 for p in P if p["fee_apr_24h"] > 100)
    n_high_flag = sum(1 for p in P if p["fee_apr_24h"] > 100 and p["red_flags"])
    ratio_bc = [p["fee_lvr_ratio"] for p in ok if p.get("fee_lvr_ratio") and p["tvl"] >= 2.5e6 and
                p["category"] in ("Голубая фишка/стейбл", "ETH/BTC")]
    med_r_bc = st.median(ratio_bc) if ratio_bc else None
    usdc_aero = next((p for p in d["llama_dex"] if p["project"] == "aerodrome-v1" and p["symbol"] == "USDC-AERO"), None)

    def btv(b, f="net_vs_hodl_apr"):
        return pct(b[f], 1, True) if b else "—"

    # ------------------------------------------------------------------ HTML
    body = f"""
<header class="top">
  <div class="wrap">
    <p class="eyebrow">Аналитика LP-пулов · данные Krystal Cloud API, DefiLlama, GeckoTerminal, DexScreener, RPC · срез {asof}</p>
    <h1>Где LP на Arbitrum и Base реально зарабатывают</h1>
    <p class="lede">{len(P)} пулов с TVL от $10 тыс (Krystal), почасовая история за 90 дней по {sum(1 for p in P if p["hist_days"])} пулам,
    тики ликвидности по {sum(1 for p in P if p["conc_eff"] and p["is_cl"])} CL-пулам и {len(bt)} прогонов бэктеста диапазонов по {len({b["key"] for b in bt})} пулам
    с учётом реальных комиссий Krystal за авто-ребаланс. Blast — для сравнения.</p>
  </div>
  <nav class="toc wrap" aria-label="Разделы">
    <a href="#summary">Главное</a><a href="#earn">1. Где зарабатывают</a><a href="#dex">2. DEX и пары</a>
    <a href="#risk">3. Риски</a><a href="#strat">4. Стратегии</a><a href="#reco">5. Рекомендации</a>
    <a href="#blast">Blast</a><a href="#method">Методика</a>
  </nav>
</header>

<main class="wrap">

<section id="summary">
  <div class="tiles">
    <div class="tile"><span class="tl">Объём DEX за 30 дней</span><span class="tv">{usd(k["base_vol"], 1)}</span><span class="ts">Base · у Arbitrum {usd(k["arb_vol"], 1)}, у Blast {usd(k["blast_vol"], 1)}</span></div>
    <div class="tile"><span class="tl">Комиссии DEX за 30 дней</span><span class="tv">{usd(k["base_fees"], 1)}</span><span class="ts">Base · у Arbitrum {usd(k["arb_fees"], 1)}</span></div>
    <div class="tile"><span class="tl">ETH за 90 / 30 дней</span><span class="tv">{pct(k["eth90"], 0, True)}</span><span class="ts">{pct(k["eth30"], 0, True)} за 30 дней: сильный тренд, неблагоприятный для LP</span></div>
    <div class="tile"><span class="tl">Медиана R по ETH/BTC-пулам</span><span class="tv">{num(med_r_bc)}</span><span class="ts">R &lt; 1: комиссии не покрывают ожидаемый IL пассивной позиции</span></div>
  </div>
  <div class="callout">
    <h2>Главное за 30 секунд</h2>
    <ol>
      <li><b>Деньги — на Base.</b> Объём Base в ~{k["base_vol"] / k["arb_vol"]:.0f} раз больше Arbitrum; больше всего комиссий генерируют Aerodrome Slipstream и Uniswap v3/v4 на Base. Blast фактически мёртв: {usd(k["blast_vol"])} объёма за месяц.</li>
      <li><b>Высокая «fee APR» не равна прибыли.</b> По голубым фишкам (ETH/USDC, BTC/USDC, ETH/BTC) комиссии полнодиапазонной позиции покрывают лишь ~{(med_r_bc or 0) * 100:.0f}% ожидаемого IL (R={num(med_r_bc)}). Средний пассивный LP за 90 дней отстал от простого холда.</li>
      <li><b>Узкий диапазон с авто-ребалансом в тренде разрушает капитал.</b> ETH/USDC ±1% с ребалансом за 90 дней: комиссии {pct(b_eth_auto1["fee_apr"] if b_eth_auto1 else None, 0)} годовых, а итог относительно холда {btv(b_eth_auto1)} годовых. В боковике последних 30 дней ±10% с ребалансом дал {btv(b_eth_auto10_30)} к холду.</li>
      <li><b>Неотрицательный результат к холду на обоих окнах показали только стейбл-пары и BTC-обёртки</b> (WBTC/cbBTC): +0.5–2% годовых. LST-пары около нуля: LST дорожает к ETH, и узкий диапазон уплывает. На Base стейблы с AERO дают 5–10%. Стейблы с эмиссией 50%+ несут риск депега: msUSD/USDC за 90 дней падал до {msusd["price_min"] if msusd else 0:.2f}.</li>
      <li><b>Эмиссия AERO — единственный крупный «бесплатный» источник</b>, но рекламные 60–80% APR получает ликвидность в диапазонах ±0.5–1%. В бэктесте ширина ±5–10% давала AERO {pct(b_aero10["reward_apr"] if b_aero10 else None, 0)}–{pct(b_aero5["reward_apr"] if b_aero5 else None, 0)} годовых, и IL перекрыл эти награды.</li>
    </ol>
  </div>
</section>

<section id="earn">
  <h2><span class="num">1</span> Где и на чём зарабатывают</h2>
  <p>Первым делом нужно разделить три вещи, которые сервисы показывают одной цифрой APR. <b>Fee APR</b> — это комиссии трейдеров, делённые на TVL пула: реальный денежный поток. <b>Эмиссионная APR</b> — это награды в токене протокола (на Base это почти всегда AERO от Aerodrome gauges): они зависят от голосования veAERO и цены AERO. <b>Чистый результат LP</b> — это комиссии плюс награды минус impermanent loss. Ниже разобраны все три.</p>

  <h3>Объём торгов по сетям, 90 дней</h3>
  <div class="chart-box"><canvas id="chVol" aria-label="Дневной объём DEX: Base, Arbitrum, Blast" role="img"></canvas></div>
  <p class="note">Источник: DefiLlama /overview/dexs. Base держит {usd(chains["Base"]["vol_24h"])} в сутки, Arbitrum {usd(chains["Arbitrum"]["vol_24h"])}, Blast {usd(chains["Blast"]["vol_24h"], 0)}.</p>

  <h3>Топ пулов по устойчивой fee APR — Base (TVL ≥ $1 млн, без красных флагов)</h3>
  <p>«Устойчивая» APR — минимум из APR за 7 дней, за 30 дней и медианы дневных значений за 90 дней. Так отсекаются разовые всплески объёма. Колонка R показывает, покрывают ли комиссии ожидаемый IL (объяснение в разделе 3).</p>
  {table(top_cols, top_fee_rows("Base"))}

  <h3>Топ пулов по устойчивой fee APR — Arbitrum</h3>
  {table(top_cols, top_fee_rows("Arbitrum"))}

  <h3>Эмиссионная доходность: Aerodrome gauges</h3>
  <p>На Aerodrome Slipstream LP выбирает одно из двух. <b>Без стейка</b> позиция получает торговые комиссии. <b>Со стейком в gauge</b> она получает AERO, а её долю комиссий забирают голосующие veAERO. Складывать fee APR и AERO APR нельзя. По ончейн-данным (stakedLiquidity) в стейке 80–99% активной ликвидности, поэтому реальная AERO APR на застейканный доллар немного выше, чем в Krystal. Эмиссия Aerodrome — около {usd(sum(p["reward_usd_day"] for p in P) * 365)} в год только по пулам Krystal.</p>
  {table(aero_cols, aero_rows)}

  <h3>Пулы вне покрытия Krystal (DefiLlama)</h3>
  <p>Krystal не отдаёт классические пулы Aerodrome v2 (vAMM/sAMM), Camelot v2, Curve и Fluid. Для полноты картины — крупнейшие из них.{" Например, USDC/AERO на Aerodrome v2: TVL " + usd(usdc_aero["tvlUsd"]) + ", награды " + pct(usdc_aero["apyReward"]) + "." if usdc_aero else ""}</p>
  {table(ll_cols, ll_rows)}
</section>

<section id="dex">
  <h2><span class="num">2</span> Какие DEX и пары дают лучший доход относительно риска</h2>
  <h3>DEX: TVL, комиссии и доходность</h3>
  {table(dex_cols, dex_rows)}
  <p class="note">Взвешенная fee APR — сумма комиссий за 7 дней в годовом выражении, делённая на суммарный TVL DEX. Высокие значения у Uniswap v4 и Aerodrome дают мем-пулы с комиссией 1%+; медиана по пулам с TVL больше $1 млн честнее отражает типичный пул.</p>

  <h3>Доходность против волатильности пары</h3>
  <p>Каждая точка — пул с TVL ≥ $1 млн. По горизонтали годовая волатильность цены пары σ, по вертикали устойчивая fee APR (логарифмическая шкала). Высокая APR почти всегда приходит вместе с высокой волатильностью. Исключения в левом верхнем углу (высокая APR при низкой σ) и есть лучшие пулы по соотношению доход/риск.</p>
  <div class="chart-box tall"><canvas id="chScatter" aria-label="Диаграмма рассеяния: fee APR против волатильности" role="img"></canvas></div>

  <h3>По типам пар</h3>
  {table(cat_cols, cat_rows)}
  <div class="grid2">
    <div><h4>Лучшее соотношение доход/риск</h4>
    <ul class="tight">
      <li><b>Стейблы на Arbitrum</b> (USDC/USD₮0 Uniswap v3 0.01%): fee APR {pct(arb_stab["fee_apr_30d"] if arb_stab else None)}, σ≈0, бэктест ±0.05% — {btv(b_stab)} годовых к холду. Мало, но надёжно.</li>
      <li><b>WETH/cbBTC на Base</b> (Uniswap v3 0.05%): fee APR {pct(weth_cbbtc["fee_apr_30d"] if weth_cbbtc else None)}, σ пары {pct((weth_cbbtc["sigma"] or 0) * 100, 0) if weth_cbbtc else "—"}. Это лучшая «волатильная» пара: ±5% с авто-ребалансом дал {btv(b_cbbtc5)} к холду за 90 дней и {btv(b_cbbtc5_30)} пассивно за 30 дней.</li>
      <li><b>Uniswap v3 Base WETH/USDC 0.05%</b>: fee APR {pct(base_eth_005["fee_apr_30d"] if base_eth_005 else None)} при TVL {usd(base_eth_005["tvl"] if base_eth_005 else None)}; работает при широком диапазоне и спокойном рынке.</li>
    </ul></div>
    <div><h4>Худшее соотношение</h4>
    <ul class="tight">
      <li><b>Мем- и long-tail-пулы с APR 100–1000%</b>: σ 150–400% годовых, IL-порог σ²/8 = 30–200% годовых, TVL мал и нестабилен.</li>
      <li><b>Токенизированные акции на Aerodrome</b> (NVDAC, TSLAC, MSTRC…): эмиссия 50–120%, но цена «гуляет» вне торговых часов биржи и ликвидность тонкая.</li>
      <li><b>Крупные альты против ETH</b> (ARB, VVV, AERO): за 30 дней −27…−34% к ETH. IL съел комиссии с запасом.</li>
    </ul></div>
  </div>
</section>

<section id="risk">
  <h2><span class="num">3</span> Риски: IL, волатильность, устойчивость APR и TVL</h2>
  <h3>Покрывают ли комиссии impermanent loss: коэффициент R</h3>
  <p>Ожидаемая годовая потеря полнодиапазонной позиции относительно холда из-за перебалансировки арбитражёрами (LVR, примерно равная ожидаемому IL) равна <span class="mono">σ²/8</span>. Из тиков Krystal я взял активную ликвидность у текущей цены и посчитал, какую fee APR получила бы полнодиапазонная позиция. <b>R = fee APR полного диапазона ÷ σ²/8.</b> При R &gt; 1 комиссии в среднем компенсируют IL; при R &lt; 1 LP в среднем проигрывает холду, и концентрация диапазона этого не исправляет: она в равной мере умножает и комиссии, и IL.</p>
  <div class="chart-box tall"><canvas id="chR" aria-label="Коэффициент R по крупнейшим пулам" role="img"></canvas></div>
  <p class="note">R — грубая оценка: активная ликвидность берётся на момент среза, σ — по дневным доходностям за 90 дней, а период совпал с сильным ростом ETH. R &gt; 1 встречается в основном у пулов с комиссией 0.3–1% и органическим потоком (CRV/WETH, AAVE/WETH, VIRTUAL/WETH v2) и у мемов, где огромные комиссии идут вместе с огромным IL.</p>

  <h3>Устойчивость fee APR во времени</h3>
  <p>Дневная fee APR за 90 дней по крупнейшим пулам. У голубых фишек APR колеблется в 2–3 раза вслед за волатильностью рынка: всплески объёма приходятся на резкие движения цены, и именно тогда LP несёт IL. Коэффициент вариации дневной APR входит в риск-балл.</p>
  <div class="chart-box"><canvas id="chApr" aria-label="Дневная fee APR за 90 дней" role="img"></canvas></div>

  <h3>Подозрительно высокие APR</h3>
  <p>Из {len(P)} пулов у {n_high} fee APR за 24 часа выше 100%, и у {n_high_flag} из них есть хотя бы один красный флаг. Всего флаги стоят на {n_flagged} пулах. Признаки: микро-TVL, разовый всплеск (APR за 24 часа в 4+ раза выше 30-дневной), объём/TVL больше 10 в сутки (wash-trading или MEV-петли), пул моложе 3 недель, неподтверждённая цена токена (WETH/POD показывает TVL $213 млн при $8 реального WETH в пуле), депег стейблкоина.</p>
  {table(sus_cols, sus_rows)}

  <h3>Сверка источников</h3>
  <p>TVL и объём по крупнейшим пулам Krystal сверены с GeckoTerminal, DexScreener, DefiLlama и ончейн-балансами токенов на контрактах пулов (RPC Arbitrum и Base). Расхождения по TVL для Uniswap v3 обычно в пределах ±1%. Для Aerodrome Slipstream до 10–15%: DexScreener и Gecko по-разному учитывают застейканную ликвидность. Fee APR Krystal близка к apyBase DefiLlama, кроме Aerodrome: DefiLlama показывает только ту долю комиссий, что остаётся LP вне стейка.</p>
  {table(xc_cols, xc_rows)}
</section>

<section id="strat">
  <h2><span class="num">4</span> Стратегии: бэктест на реальной истории</h2>
  <p>Позиция $10 000 моделируется по часам на истории цены, комиссий и TVL пула за 90 и за 30 дней. Доля позиции в комиссиях равна её доле в активной ликвидности; ликвидность пула масштабируется вместе с TVL. Авто-ребаланс срабатывает при выходе цены из диапазона. Его стоимость: тариф Krystal 0.01/0.03/0.05% от позиции, своп половины позиции по комиссии пула плюс 0.02% проскальзывания и $0.10 газа. Результат показан <b>относительно холда</b> исходных токенов, в процентах годовых: это «альфа» LP. Доходность в долларах дополнительно включает рост самих токенов.</p>

  <div class="grid2">
    <div><h3>ETH/USDC: ширина диапазона, 90 дней (ETH {pct(k["eth90"], 0, True)})</h3>
    <div class="chart-box"><canvas id="chBt90" aria-label="Бэктест ETH/USDC за 90 дней" role="img"></canvas></div></div>
    <div><h3>ETH/USDC: ширина диапазона, 30 дней (ETH {pct(k["eth30"], 0, True)})</h3>
    <div class="chart-box"><canvas id="chBt30" aria-label="Бэктест ETH/USDC за 30 дней" role="img"></canvas></div></div>
  </div>
  <p class="note">Пул: Uniswap v3 Base WETH/USDC 0.3%. По вертикали результат к холду, % годовых; во всплывающей подсказке — комиссии, время в диапазоне и число ребалансов.</p>

  <h3>Aerodrome WETH/USDC: комиссии без стейка против AERO в стейке (30 дней)</h3>
  <div class="chart-box"><canvas id="chAero" aria-label="Бэктест Aerodrome WETH/USDC" role="img"></canvas></div>

  <h3>Сводка бэктестов по всем выбранным пулам</h3>
  {table(bt_cols, bt_rows)}

  <div class="strats">
    <article><h4>Стейбл-пары (USDC/USDT, USDC/USD₮0, EURC/USDC)</h4>
      <p><b>Доход:</b> 1–4% годовых комиссиями на Arbitrum/Base; на Aerodrome со стейком +2–10% в AERO (EURC/USDC, USDC/USDT). <b>Риски:</b> депег (msUSD/USDC падал до ~0.46 при эмиссии 52%), смарт-контракт. <b>Настройка:</b> ±0.05–0.1% без авто-ребаланса: стейблы возвращаются в диапазон сами, а частые ребалансы съедают доход.</p></article>
    <article><h4>ETH-корреляционные пары (wstETH/WETH, cbETH/WETH, weETH/WETH)</h4>
      <p><b>Доход:</b> пулы платят 0.5–2% комиссиями и 3–11% AERO на TVL (Base), но почти вся ликвидность стоит в 1–2 тиках. Позиция ±0.25–0.5% получала в бэктесте 0.1–1% годовых и за 90 дней отстала от холда на 0.2–1.4%. <b>Риски:</b> LST дорожает к ETH примерно на 3% в год, поэтому узкий диапазон уплывает; конкуренция с профессиональными 1-тиковыми позициями. <b>Вывод:</b> для обычного LP простой холд wstETH выгоднее. Пары имеют смысл только с плотной автоматизацией под AERO.</p></article>
    <article><h4>Концентрированная ликвидность ETH/USDC, BTC/USDC, ETH/BTC</h4>
      <p><b>Доход:</b> 10–75% комиссий в годовых на ±5–10%, но к холду от {btv(b_eth_auto1)} (±1%, тренд) до {btv(b_eth_auto10_30)} (±10%, боковик). <b>Правило:</b> узко — только в боковике, широко (±10–20%) — в тренде. ETH/cbBTC на Base (σ≈22%, высокий оборот) прощает ошибки лучше всех: ±10–20% дали от −5% до +11% к холду на обоих окнах. «Лучшая настройка» в таблице выбрана задним числом и на другом окне часто проигрывает.</p></article>
    <article><h4>Авто-ребаланс и автоматизация Krystal</h4>
      <p>Тарифы: авто-ребаланс 0.01–0.05% от позиции, авто-компаунд 2% от комиссий, zap 0.05–0.25%. Главная стоимость ребаланса — не тариф, а фиксация IL и своп половины позиции. Ребаланс оправдан при ширине от ±5% и при R пула около 1 и выше; при ±1–2.5% в тренде он превращает 100–300% комиссий в минус. Полезно: auto-exit по цене как стоп и авто-компаунд на стейблах.</p></article>
    <article><h4>Фарминг AERO на Aerodrome</h4>
      <p><b>Доход:</b> по пулам ETH/USDC и ETH/cbBTC 60–85% AERO на застейканный TVL, но эта цифра — средняя по ликвидности, стоящей в ±0.1–1% (эффективная концентрация 100–450×). В бэктесте ±5–10% ETH/USDC получал {pct(b_aero10["reward_apr"] if b_aero10 else None, 0)}–{pct(b_aero5["reward_apr"] if b_aero5 else None, 0)}, а ETH/cbBTC ±5–10% — лишь 3–11%. <b>Риски:</b> AERO −33% за 30 дней (награды надо продавать регулярно), IL, перераспределение эмиссии голосованием каждую эпоху (неделю). Классический USDC/AERO v2 даёт ~{pct(usdc_aero["apyReward"] if usdc_aero else None, 0)} AERO, но половина позиции — сам AERO.</p></article>
    <article><h4>Мемы и новые пулы с комиссией 1%</h4>
      <p><b>Доход:</b> 100–1000% fee APR. <b>Риски:</b> σ 150–400%, ожидаемый IL сопоставим с комиссиями или больше, токен может обнулиться, TVL уходит за дни. Это спекуляция на токене с бонусом в виде комиссий, а не доходная стратегия.</p></article>
  </div>
</section>

<section id="reco">
  <h2><span class="num">5</span> Рекомендации по профилям</h2>
  <div class="profiles">
    <article class="prof p1"><h3>Консервативный</h3><p class="exp">Цель 3–8% годовых в долларах, без ценового риска</p>
      <ul>
        <li>60–70%: USDC/USD₮0 (Arbitrum, Uniswap v3 0.01%) или USDC/USDT (Base), диапазон ±0.05–0.1%, без авто-ребаланса, авто-компаунд раз в 1–2 недели.</li>
        <li>20–30%: USDC/USDT на Aerodrome со стейком в gauge (комиссии плюс AERO); AERO продавать еженедельно. EURC/USDC даёт больше, но несёт курсовой риск EUR/USD.</li>
        <li>Если нужна ETH-экспозиция — держать wstETH напрямую (~3% стейкинга), а не LP wstETH/WETH.</li>
        <li>Не заходить в «стейблы» с эмиссией 30%+ без истории пега (пример — msUSD).</li>
      </ul></article>
    <article class="prof p2"><h3>Умеренный</h3><p class="exp">Цель 10–25% годовых, частичная экспозиция на ETH/BTC</p>
      <ul>
        <li>40%: WETH/cbBTC (Base, Uniswap v3 0.05%), ±10–20%, авто-ребаланс Krystal только при выходе из диапазона. Ожидание: 7–20% комиссиями, около нуля или плюс к холду.</li>
        <li>30%: ETH/USDC на Base (Uniswap v3 0.05% или Aerodrome WETH/USDC со стейком), ±10–20%; сужать до ±5% только в боковике.</li>
        <li>30%: консервативная стейбл-корзина как буфер.</li>
        <li>Раз в неделю сравнивать позицию с холдом во вкладке Krystal (PnL vs HODL); при R &lt; 0.5 в пуле переходить на более широкий диапазон.</li>
      </ul></article>
    <article class="prof p3"><h3>Агрессивный</h3><p class="exp">Цель 30%+ годовых, готовность к −30…−50%</p>
      <ul>
        <li>Фарминг AERO в ETH/USDC, ETH/cbBTC, USDC/cbBTC Slipstream: ±2–5% с авто-ребалансом только при низкой волатильности, AERO продавать сразу.</li>
        <li>Пулы крупных альтов с органическим потоком и R около 1 (VIRTUAL/WETH, CRV/WETH, AAVE/WETH): ±20–40%.</li>
        <li>Мем-пулы с комиссией 1% — только малой долей (≤5–10%), только с TVL больше $1 млн и историей больше 30 дней, с auto-exit по цене.</li>
        <li>Не использовать ±1–2.5% с авто-ребалансом в трендовом рынке: в бэктесте это −100…−500% годовых к холду.</li>
      </ul></article>
  </div>
</section>

<section id="blast">
  <h2>Blast — для сравнения</h2>
  <p>Krystal Blast не поддерживает; данные из DefiLlama и GeckoTerminal. Объём DEX за 30 дней — {usd(k["blast_vol"])} против {usd(k["base_vol"])} на Base, то есть примерно в {k["base_vol"] / max(k["blast_vol"], 1):,.0f} раз меньше. Комиссии — {usd(k["blast_fees"])} за месяц на всю сеть. DefiLlama Yields не отслеживает на Blast ни одного LP-пула. Пулов с TVL больше $100 тыс в топе GeckoTerminal — {blast.get("n_pools_over_100k")}, крупнейший по TVL (YES/WETH, «$153 млн») — артефакт оценки при объёме $21 в сутки. Сеть жива (блок {blast.get("rpc_block") or "—"} по RPC), но для LP Blast сейчас неинтересен.</p>
  {table(bl_cols, bl_rows)}
</section>

<section id="method">
  <h2>Методика и ограничения</h2>
  <ul class="tight">
    <li><b>Источники:</b> Krystal Cloud API (/v1/pools с incentives и ценами токенов, /historical — почасовая история за 90 дней, /ticks — распределение ликвидности), DefiLlama (yields, overview dexs/fees, история APY), GeckoTerminal и DexScreener (сверка TVL и объёма), RPC Arbitrum/Base (liquidity(), stakedLiquidity(), балансы токенов на пулах).</li>
    <li><b>Очистка TVL:</b> если одна сторона пула — стейбл, ETH, BTC или крупный токен, а вторая (альт) оценена больше чем в 10 раз дороже, TVL принимается равным удвоенной «доверенной» стороне. Пулы с TVL больше $2 млн без объёма помечаются как мёртвые.</li>
    <li><b>σ</b> считается по дневным лог-доходностям цены пула за 90 дней: это устойчиво к шуму внутри комиссионного коридора. <b>IL-порог</b> σ²/8 — ожидаемая годовая потеря полнодиапазонной позиции к холду (LVR).</li>
    <li><b>Риск-балл 1–10:</b> волатильность, размер TVL, нестабильность APR, отток TVL, long-tail-токены, флаги данных, возраст пула.</li>
    <li><b>Бэктест</b> исходит из того, что позиция мала относительно пула и что эмиссия AERO в $/день постоянна (на уровне среза); он не учитывает MEV/JIT-ликвидность и задержку срабатывания автоматизации. Результат одного 90-дневного окна с сильным ростом ETH не переносится на любой рынок — поэтому рядом показано 30-дневное окно.</li>
    <li>Это аналитика, а не инвестиционная рекомендация. Скрипты для перезапуска лежат в репозитории: <span class="mono">collect.py → analyze.py → build_report.py</span>.</li>
  </ul>
</section>
</main>
"""
    page = TEMPLATE.replace("%%BODY%%", body).replace("%%DATA%%", json.dumps(payload, ensure_ascii=False))
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(page)
    print(f"written {OUT} ({len(page) / 1024:.0f} KB)")


TEMPLATE = r"""<title>LP-пулы Arbitrum и Base</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Unbounded:wght@500;700&family=Onest:wght@400;500;700&family=JetBrains+Mono:wght@400;600&display=swap">
<style>
/* Layout: длинное аналитическое чтение, 1 колонка ~1100px, липкая навигация по 5 вопросам; таблицы — в своих scroll-контейнерах */
:root{
  --bg:#f5f7f8; --surface:#ffffff; --ink:#132029; --ink2:#46555f; --muted:#6c7a84; --line:#dde3e7;
  --accent:#0b6e66; --accent-soft:#e3f1ef;
  --good:#1b7f45; --good-bg:#e4f4ea; --warn:#9a6200; --warn-bg:#fbf0d9; --bad:#b4332f; --bad-bg:#fbe5e3;
  --s1:#2a78d6; --s2:#eb6834; --s3:#1baf7a; --s4:#eda100; --grid:#e7ecef;
  --f-display:"Unbounded","Onest",system-ui,sans-serif; --f-body:"Onest",system-ui,-apple-system,"Segoe UI",sans-serif;
  --f-mono:"JetBrains Mono",ui-monospace,"SFMono-Regular",Menlo,monospace;
}
@media (prefers-color-scheme: dark){:root:not([data-theme="light"]){
  --bg:#0f161b; --surface:#151f26; --ink:#e6edf1; --ink2:#b3c0c8; --muted:#8a99a3; --line:#26343d;
  --accent:#4fc3b5; --accent-soft:#16312f;
  --good:#5fd08f; --good-bg:#16301f; --warn:#f0b54a; --warn-bg:#33270f; --bad:#f08079; --bad-bg:#3a1b19;
  --s1:#3987e5; --s2:#d95926; --s3:#199e70; --s4:#c98500; --grid:#22303a; color-scheme:dark}}
:root[data-theme="dark"]{
  --bg:#0f161b; --surface:#151f26; --ink:#e6edf1; --ink2:#b3c0c8; --muted:#8a99a3; --line:#26343d;
  --accent:#4fc3b5; --accent-soft:#16312f;
  --good:#5fd08f; --good-bg:#16301f; --warn:#f0b54a; --warn-bg:#33270f; --bad:#f08079; --bad-bg:#3a1b19;
  --s1:#3987e5; --s2:#d95926; --s3:#199e70; --s4:#c98500; --grid:#22303a; color-scheme:dark}
*{box-sizing:border-box}
body{background:var(--bg);color:var(--ink);font:15px/1.6 var(--f-body);margin:0}
.wrap{max-width:1120px;margin:0 auto;padding-inline:20px}
header.top{background:var(--surface);border-bottom:1px solid var(--line)}
header.top .wrap:first-child{padding-block:36px 18px}
.eyebrow{font:500 12px/1.4 var(--f-mono);letter-spacing:.04em;color:var(--accent);margin:0 0 12px;text-transform:uppercase}
h1{font:700 clamp(26px,4vw,40px)/1.15 var(--f-display);margin:0 0 14px;text-wrap:balance;letter-spacing:-.01em}
.lede{color:var(--ink2);max-width:72ch;margin:0}
.toc{display:flex;flex-wrap:wrap;gap:4px 18px;padding-block:10px;position:sticky;top:env(safe-area-inset-top,0px);background:var(--surface);z-index:5;border-top:1px solid var(--line);font-size:14px}
.toc a{white-space:nowrap;color:var(--ink2);text-decoration:none;padding:4px 0;border-bottom:2px solid transparent}
.toc a:hover,.toc a:focus-visible{color:var(--accent);border-bottom-color:var(--accent);outline:none}
main{padding-block:28px 64px}
section{padding-block:22px;border-bottom:1px solid var(--line);scroll-margin-top:60px}
section:last-child{border-bottom:0}
h2{font:700 clamp(20px,2.6vw,26px)/1.25 var(--f-display);margin:8px 0 14px;text-wrap:balance;display:flex;gap:12px;align-items:baseline}
h2 .num{font:600 14px var(--f-mono);color:var(--accent);border:1.5px solid var(--accent);border-radius:6px;padding:1px 7px;flex:none}
h3{font:700 17px/1.35 var(--f-body);margin:28px 0 8px;text-wrap:balance}
h4{font:700 15px/1.35 var(--f-body);margin:0 0 6px}
p{max-width:78ch;margin:0 0 12px}
.note{color:var(--muted);font-size:13px}
.mono{font-family:var(--f-mono);font-size:.92em}
.muted{color:var(--muted)}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:12px;margin-bottom:18px}
.tile{background:var(--surface);border:1px solid var(--line);border-radius:10px;padding:14px 16px;display:flex;flex-direction:column;gap:4px;min-width:0}
.tl{font:500 12px var(--f-mono);color:var(--muted);text-transform:uppercase;letter-spacing:.04em}
.tv{font:700 26px/1.2 var(--f-display);font-variant-numeric:tabular-nums}
.ts{font-size:13px;color:var(--ink2)}
.callout{background:var(--accent-soft);border-radius:12px;padding:18px 22px}
.callout h2{margin-top:0;font-size:20px}
.callout ol{margin:0;padding-left:20px;display:grid;gap:8px;max-width:90ch}
.tw{overflow-x:auto;margin:10px 0 8px;border:1px solid var(--line);border-radius:10px;background:var(--surface)}
table{border-collapse:collapse;width:100%;font-size:13.5px;font-variant-numeric:tabular-nums}
th,td{padding:8px 10px;border-bottom:1px solid var(--line);vertical-align:top;text-align:left}
th{font:600 12px/1.3 var(--f-body);color:var(--ink2);background:var(--bg);position:sticky;top:0;white-space:nowrap}
td.num,th.num{text-align:right;white-space:nowrap}
tbody tr:last-child td{border-bottom:0}
tbody tr:hover{background:var(--bg)}
table.sortable th{cursor:pointer;user-select:none}
table.sortable th:hover{color:var(--accent)}
th[aria-sort="descending"]::after{content:" ↓"}
th[aria-sort="ascending"]::after{content:" ↑"}
.sub{font-size:12px;color:var(--muted);font-weight:400}
.chip{display:inline-block;padding:1px 8px;border-radius:99px;font:600 12px var(--f-mono)}
.chip.ok{background:var(--good-bg);color:var(--good)}
.chip.mid{background:var(--warn-bg);color:var(--warn)}
.chip.hi{background:var(--bad-bg);color:var(--bad)}
.flag{display:inline-block;margin:1px 2px 1px 0;padding:1px 7px;border-radius:5px;background:var(--bad-bg);color:var(--bad);font-size:12px;white-space:nowrap}
.chart-box{position:relative;height:300px;background:var(--surface);border:1px solid var(--line);border-radius:10px;padding:12px;margin:10px 0}
.chart-box.tall{height:420px}
.grid2{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:18px}
.grid2>div{min-width:0}
ul.tight{padding-left:20px;margin:0 0 10px;display:grid;gap:6px;max-width:90ch}
.strats{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:14px;margin-top:22px}
.strats article{background:var(--surface);border:1px solid var(--line);border-radius:10px;padding:14px 16px;min-width:0}
.strats p{font-size:14px;margin:0}
.profiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:14px}
.prof{background:var(--surface);border:1px solid var(--line);border-radius:12px;padding:16px 18px;min-width:0;border-top:4px solid var(--line)}
.prof.p1{border-top-color:var(--good)} .prof.p2{border-top-color:var(--warn)} .prof.p3{border-top-color:var(--bad)}
.prof h3{margin:0 0 2px;font-family:var(--f-display);font-size:18px}
.prof .exp{color:var(--muted);font-size:13px;margin-bottom:10px}
.prof ul{padding-left:18px;margin:0;display:grid;gap:8px;font-size:14px}
a{color:var(--accent)}
:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
@media (max-width:560px){.toc{flex-wrap:nowrap;overflow-x:auto;scrollbar-width:none}.wrap{padding-inline:16px}.chart-box{height:260px}.chart-box.tall{height:340px}.tv{font-size:22px}}
@media (prefers-reduced-motion:reduce){*{transition:none!important;animation:none!important}}
</style>
%%BODY%%
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>
<script>
const D = %%DATA%%;
const css = n => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
const fmtUsd = v => v>=1e9 ? '$'+(v/1e9).toFixed(2)+' млрд' : v>=1e6 ? '$'+(v/1e6).toFixed(1)+' млн' : '$'+Math.round(v/1e3)+' тыс';
const charts = [];
function base(){
  return {responsive:true, maintainAspectRatio:false, animation:false,
    interaction:{mode:'nearest', intersect:false},
    plugins:{legend:{labels:{color:css('--ink2'), boxWidth:12, boxHeight:12, font:{family:'Onest, system-ui, sans-serif'}}},
      tooltip:{backgroundColor:css('--surface'), titleColor:css('--ink'), bodyColor:css('--ink2'), borderColor:css('--line'), borderWidth:1, padding:10}},
    scales:{x:{grid:{color:css('--grid')}, ticks:{color:css('--muted')}, border:{color:css('--line')}},
            y:{grid:{color:css('--grid')}, ticks:{color:css('--muted')}, border:{color:css('--line')}}}};
}
function render(){
  charts.forEach(c=>c.destroy()); charts.length=0;
  if(!window.Chart) return;
  Chart.defaults.font.family='Onest, system-ui, sans-serif';
  const S=[css('--s1'),css('--s2'),css('--s3'),css('--s4')];
  // 1. объём по сетям
  {const o=base(); const ser=['Base','Arbitrum','Blast'];
   o.scales.x={...o.scales.x, type:'category', ticks:{color:css('--muted'), maxTicksLimit:8}};
   o.scales.y.ticks.callback=v=>fmtUsd(v); o.plugins.tooltip.callbacks={label:c=>c.dataset.label+': '+fmtUsd(c.parsed.y)};
   o.interaction={mode:'index', intersect:false};
   const labels=D.chains.Base.map(p=>new Date(p[0]*1000).toLocaleDateString('ru-RU',{day:'2-digit',month:'2-digit'}));
   charts.push(new Chart(document.getElementById('chVol'),{type:'line',data:{labels,datasets:ser.map((s,i)=>({label:s,data:D.chains[s].map(p=>p[1]),borderColor:S[i],backgroundColor:S[i],borderWidth:2,pointRadius:0,tension:.2}))},options:o}));}
  // 2. scatter
  {const o=base(); const names=['Стейблы и коррелированные','ETH/BTC и голубые фишки к стейблу','Альты, мемы, акции'];
   o.scales.x.title={display:true,text:'Волатильность пары σ, % годовых',color:css('--muted')};
   o.scales.y={...o.scales.y,type:'logarithmic',title:{display:true,text:'Устойчивая fee APR, % (лог.)',color:css('--muted')},ticks:{color:css('--muted'),callback:v=>[0.1,0.3,1,3,10,30,100,300,1000].includes(v)?v+'%':''}};
   o.interaction={mode:'nearest',intersect:true};
   o.plugins.tooltip.callbacks={label:c=>{const r=c.raw;return [r.n,'APR '+r.y+'%, σ '+r.x+'%, TVL '+fmtUsd(r.t)];}};
   const ds=[0,1,2].map(g=>({label:names[g],data:D.scatter.filter(p=>p.g===g),backgroundColor:S[g]+'cc',borderColor:css('--surface'),borderWidth:1,pointRadius:c=>{const t=c.raw?c.raw.t:1e6;return Math.max(4,Math.min(14,Math.log10(t)*2.2-9));},pointHoverRadius:9}));
   charts.push(new Chart(document.getElementById('chScatter'),{type:'scatter',data:{datasets:ds},options:o}));}
  // 3. R bars
  {const o=base(); o.indexAxis='y'; o.plugins.legend.display=false;
   o.scales.x.title={display:true,text:'R = fee APR полного диапазона ÷ σ²/8',color:css('--muted')}; o.scales.x.min=0; o.scales.x.suggestedMax=Math.max(1.15,...D.rbars.map(r=>r.r))*1.05;
   o.scales.y.ticks={color:css('--ink2'),autoSkip:false,font:{size:11}};
   o.plugins.tooltip.callbacks={label:c=>{const r=D.rbars[c.dataIndex];return ['R = '+r.r,'fee APR полного диапазона '+r.fee+'%','ожидаемый IL σ²/8 = '+r.lvr+'%'];}};
   const ref={id:'ref',afterDraw(ch){const x=ch.scales.x.getPixelForValue(1);const a=ch.chartArea;const g=ch.ctx;g.save();g.strokeStyle=css('--ink2');g.setLineDash([4,4]);g.beginPath();g.moveTo(x,a.top);g.lineTo(x,a.bottom);g.stroke();g.fillStyle=css('--ink2');g.font='12px Onest, system-ui, sans-serif';g.textAlign='right';g.fillText('R = 1: безубыточность к холду',x-6,a.bottom-8);g.restore();}};
   charts.push(new Chart(document.getElementById('chR'),{type:'bar',data:{labels:D.rbars.map(r=>r.n),datasets:[{data:D.rbars.map(r=>r.r),backgroundColor:D.rbars.map(r=>r.r>=1?css('--good'):r.r>=0.5?css('--warn'):css('--bad')),borderRadius:4,barThickness:14}]},options:o,plugins:[ref]}));}
  // 4. APR series
  {const o=base(); o.interaction={mode:'index',intersect:false};
   o.scales.y.title={display:true,text:'Fee APR за сутки, %',color:css('--muted')};
   o.plugins.tooltip.callbacks={label:c=>c.dataset.label.split(' · ')[0]+' '+c.dataset.label.split(' · ')[1]+': '+c.parsed.y+'%'};
   const n=Math.max(...D.series.map(s=>s.apr.length)); const labels=Array.from({length:n},(_,i)=>'−'+(n-1-i)+' дн');
   charts.push(new Chart(document.getElementById('chApr'),{type:'line',data:{labels,datasets:D.series.map((s,i)=>({label:s.n,data:s.apr,borderColor:S[i],backgroundColor:S[i],borderWidth:2,pointRadius:0,tension:.25}))},options:o}));}
  // 5. backtests
  function bt(id, obj, colors){
    if(!obj) return; const o=base(); o.interaction={mode:'index',intersect:false};
    const widths=[...new Set(obj.series.flatMap(s=>s.pts.map(p=>p[0])))].sort((a,b)=>a-b);
    o.scales.y.title={display:true,text:'Результат к холду, % годовых',color:css('--muted')};
    o.scales.x.title={display:true,text:'Полуширина диапазона',color:css('--muted')};
    o.plugins.tooltip.callbacks={label:c=>{const s=obj.series[c.datasetIndex];const p=s.pts.find(q=>'±'+q[0]+'%'===c.label);return p?[s.m+': '+p[1]+'% к холду','  доход (комиссии/AERO) '+p[2]+'%, в диапазоне '+p[3]+'%, ребалансов '+p[4]]:'';}};
    const zero={id:'zero',beforeDatasetsDraw(ch){const y=ch.scales.y.getPixelForValue(0);const a=ch.chartArea;if(y<a.top||y>a.bottom)return;const g=ch.ctx;g.save();g.strokeStyle=css('--ink2');g.lineWidth=1;g.beginPath();g.moveTo(a.left,y);g.lineTo(a.right,y);g.stroke();g.restore();}};
    charts.push(new Chart(document.getElementById(id),{type:'line',data:{labels:widths.map(w=>'±'+w+'%'),datasets:obj.series.map((s,i)=>({label:s.m,data:widths.map(w=>{const p=s.pts.find(q=>q[0]===w);return p?p[1]:null;}),borderColor:colors[i],backgroundColor:colors[i],borderWidth:2,pointRadius:4,pointHoverRadius:6,tension:0}))},options:o,plugins:[zero]}));
  }
  bt('chBt90', D.btEth[90], [S[0],S[1]]);
  bt('chBt30', D.btEth[30], [S[0],S[1]]);
  bt('chAero', D.btAero[30], S);
}
render();
const mq=matchMedia('(prefers-color-scheme: dark)'); mq.addEventListener&&mq.addEventListener('change',render);
new MutationObserver(render).observe(document.documentElement,{attributes:true,attributeFilter:['data-theme']});
// сортировка таблиц
document.querySelectorAll('table.sortable').forEach(t=>{
  t.querySelectorAll('th').forEach((th,i)=>{th.tabIndex=0;const go=()=>{
    const dir=th.getAttribute('aria-sort')==='descending'?'ascending':'descending';
    t.querySelectorAll('th').forEach(x=>x.removeAttribute('aria-sort')); th.setAttribute('aria-sort',dir);
    const rows=[...t.tBodies[0].rows]; const num=th.classList.contains('num');
    rows.sort((a,b)=>{const ca=a.cells[i],cb=b.cells[i];let va=ca.dataset.v??ca.textContent,vb=cb.dataset.v??cb.textContent;
      if(num){va=parseFloat(va);vb=parseFloat(vb);va=isNaN(va)?-Infinity:va;vb=isNaN(vb)?-Infinity:vb;return dir==='descending'?vb-va:va-vb;}
      return dir==='descending'?String(vb).localeCompare(va,'ru'):String(va).localeCompare(vb,'ru');});
    rows.forEach(r=>t.tBodies[0].appendChild(r));};
    th.addEventListener('click',go); th.addEventListener('keydown',e=>{if(e.key==='Enter')go();});});
});
</script>
"""

if __name__ == "__main__":
    main()
