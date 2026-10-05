"""Метрики риска по почасовой истории пула и математика концентрированной ликвидности."""
from __future__ import annotations

import math
import statistics as st

HOURS_PER_YEAR = 24 * 365


def il_full_range(price_ratio: float) -> float:
    """Impermanent loss позиции x*y=k относительно HODL при изменении цены в r раз (доля)."""
    r = price_ratio
    return 2 * math.sqrt(r) / (1 + r) - 1


def history_metrics(h: list[dict]) -> dict:
    """h — почасовые точки Krystal: timestamp, volume24h, fee24h, apr24h, tvlUsd, poolPrice."""
    h = [x for x in h if x.get("tvlUsd") and x.get("poolPrice")]
    if len(h) < 48:
        return {"hist_days": len(h) / 24}
    end = h[-1]["timestamp"]
    # дневные точки (каждые 24 ч от конца) — rolling 24h fee/TVL
    daily = [x for x in h if (end - x["timestamp"]) % 86400 == 0]
    aprs = [x["fee24h"] * 365 / x["tvlUsd"] * 100 for x in daily if x["tvlUsd"] > 0]
    tvls = [x["tvlUsd"] for x in daily]
    rets = []
    for a, b in zip(h, h[1:]):
        if b["timestamp"] - a["timestamp"] == 3600 and a["poolPrice"] > 0 and b["poolPrice"] > 0:
            rets.append(math.log(b["poolPrice"] / a["poolPrice"]))
    # отсекаем явные артефакты (скачки >40% за час на ликвидных пулах — обычно глюк цены)
    rets = [r for r in rets if abs(r) < 0.4]
    sigma_h = st.pstdev(rets) * math.sqrt(HOURS_PER_YEAR) if len(rets) > 24 else None
    # σ по суточным доходностям: устойчива к «шуму» цены внутри комиссионного коридора
    # (важно для стейблов и коррелированных пар). Её используем для оценки LVR/IL.
    dprices = [x["poolPrice"] for x in daily]
    drets = [math.log(b / a) for a, b in zip(dprices, dprices[1:]) if a > 0 and b > 0]
    drets = [r for r in drets if abs(r) < 1.0]
    sigma_d = st.pstdev(drets) * math.sqrt(365) if len(drets) >= 10 else None
    sigma = sigma_d if sigma_d is not None else sigma_h

    def at(days):
        t = end - days * 86400
        cand = [x for x in h if x["timestamp"] >= t]
        return cand[0] if cand else None

    out = {
        "hist_days": (end - h[0]["timestamp"]) / 86400,
        "apr_daily_mean": st.mean(aprs) if aprs else None,
        "apr_daily_median": st.median(aprs) if aprs else None,
        "apr_daily_p10": sorted(aprs)[len(aprs) // 10] if len(aprs) >= 10 else None,
        "apr_cv": (st.pstdev(aprs) / st.mean(aprs)) if len(aprs) > 3 and st.mean(aprs) > 0 else None,
        "tvl_cv": (st.pstdev(tvls) / st.mean(tvls)) if len(tvls) > 3 else None,
        "sigma": sigma, "sigma_hourly": sigma_h,
        "lvr_full_pct": sigma ** 2 / 8 * 100 if sigma is not None else None,
        "apr_series": [round(a, 2) for a in aprs[-90:]],
        "tvl_series": [round(t) for t in tvls[-90:]],
    }
    for d in (7, 30, 90):
        a = at(d)
        if a and (end - a["timestamp"]) >= (d - 1) * 86400:
            out[f"tvl_chg_{d}d"] = (h[-1]["tvlUsd"] / a["tvlUsd"] - 1) * 100
            r = h[-1]["poolPrice"] / a["poolPrice"]
            out[f"price_chg_{d}d"] = (r - 1) * 100
            out[f"il_{d}d"] = il_full_range(r) * 100
    # максимальная просадка цены пары (для понимания риска выхода из диапазона)
    peak, mdd = h[0]["poolPrice"], 0.0
    for x in h:
        peak = max(peak, x["poolPrice"])
        mdd = min(mdd, x["poolPrice"] / peak - 1)
    out["price_max_dd"] = mdd * 100
    med = st.median(x["poolPrice"] for x in h)
    out["price_dev_max"] = max(abs(x["poolPrice"] / med - 1) for x in h) * 100
    out["price_min"] = min(x["poolPrice"] for x in h)
    out["price_max"] = max(x["poolPrice"] for x in h)
    return out


# ------------------------------------------------------------ CL math (human units)
def amounts_for_L(L: float, P: float, pa: float, pb: float) -> tuple[float, float]:
    """Количество token0/token1 для ликвидности L в диапазоне [pa,pb] при цене P (token1 за token0)."""
    sp, sa, sb = math.sqrt(P), math.sqrt(pa), math.sqrt(pb)
    if P <= pa:
        return L * (1 / sa - 1 / sb), 0.0
    if P >= pb:
        return 0.0, L * (sb - sa)
    return L * (1 / sp - 1 / sb), L * (sp - sa)


def value_per_L(P, pa, pb):
    a0, a1 = amounts_for_L(1.0, P, pa, pb)
    return a0 * P + a1  # в единицах token1


def human_L(L_raw: int, dec0: int, dec1: int) -> float:
    return L_raw / 10 ** ((dec0 + dec1) / 2)


def krystal_auto_fee(fee_pct: float) -> float:
    """Комиссия Krystal за авто-ребаланс (доля от стоимости позиции), docs.krystal.app/ecosystem/fees."""
    if fee_pct <= 0.05:
        return 0.0001
    if fee_pct <= 0.3:
        return 0.0003
    return 0.0005


def backtest_cl(hist: list[dict], L_now: float, tvl_now: float, width: float | None,
                fee_pct: float, invert: bool, quote_usd: dict | None = None,
                rebalance: bool = False, reward_usd_day: float = 0.0, earn_fees: bool = True,
                v0: float = 10_000.0, gas_usd: float = 0.10, slippage: float = 0.0002,
                window_days: int | None = None, staked_share: float = 1.0) -> dict:
    """Бэктест позиции концентрированной ликвидности по почасовой истории.

    width  — полуширина диапазона в долях (0.05 = ±5%), None = полный диапазон.
    invert — если котируемый токен (USD/ETH) — это token0, работаем с ценой 1/P.
    Доход на единицу ликвидности: fee24h/24 / L_t, где L_t = L_now * TVL_t / TVL_now
    (активная ликвидность масштабируется вместе с TVL пула).
    reward_usd_day — эмиссия (Aerodrome gauge), распределяется на застейканную активную
    ликвидность (staked_share — её доля в активной, по ончейн stakedLiquidity()).
    earn_fees=False — застейканная позиция Aerodrome (комиссии уходят veAERO-голосующим).
    """
    pts = [x for x in hist if x.get("poolPrice") and x.get("tvlUsd")]
    if window_days:
        t0 = pts[-1]["timestamp"] - window_days * 86400 if pts else 0
        pts = [x for x in pts if x["timestamp"] >= t0]
    if len(pts) < 48:
        return {}

    def price(x):
        return 1 / x["poolPrice"] if invert else x["poolPrice"]

    def qusd(x):
        if quote_usd is None:
            return 1.0
        return quote_usd.get(x["timestamp"]) or quote_usd.get("last", 1.0)

    def make_range(P):
        if width is None:
            return P * 1e-6, P * 1e6
        return P / (1 + width), P * (1 + width)

    P0 = price(pts[0])
    pa, pb = make_range(P0)
    q0 = qusd(pts[0])
    v_quote = v0 / q0
    L = v_quote / value_per_L(P0, pa, pb)
    hold0, hold1 = amounts_for_L(L, P0, pa, pb)
    fees_usd = rewards_usd = costs_usd = 0.0
    in_range_h = 0
    n_reb = 0
    for x in pts[1:]:
        P = price(x)
        q = qusd(x)
        L_pool = L_now * x["tvlUsd"] / tvl_now if tvl_now > 0 else L_now
        if L_pool <= 0:
            continue
        inr = pa <= P <= pb
        if inr:
            in_range_h += 1
            if earn_fees:
                fees_usd += (x["fee24h"] / 24) * L / (L_pool + L)
            # эмиссия делится только между застейканной активной ликвидностью
            rewards_usd += reward_usd_day / 24 * L / (L_pool * staked_share + L)
        elif rebalance and width is not None:
            a0, a1 = amounts_for_L(L, P, pa, pb)
            v = (a0 * P + a1) * q
            cost = v * (0.5 * (fee_pct / 100 + slippage) + krystal_auto_fee(fee_pct)) + gas_usd
            costs_usd += cost
            v -= cost
            pa, pb = make_range(P)
            L = (v / q) / value_per_L(P, pa, pb)
            n_reb += 1
    last = pts[-1]
    P, q = price(last), qusd(last)
    a0, a1 = amounts_for_L(L, P, pa, pb)
    v_lp = (a0 * P + a1) * q
    v_hold = (hold0 * P + hold1) * q
    days = (last["timestamp"] - pts[0]["timestamp"]) / 86400
    ann = 365 / days
    total = v_lp + fees_usd + rewards_usd
    return {
        "days": days,
        "time_in_range": in_range_h / max(1, len(pts) - 1) * 100,
        "rebalances": n_reb,
        "fee_apr": fees_usd / v0 * ann * 100,
        "reward_apr": rewards_usd / v0 * ann * 100,
        "cost_apr": costs_usd / v0 * ann * 100,
        "il_vs_hodl_pct": (v_lp + costs_usd - v_hold) / v0 * 100,  # ценовая часть (IL/LVR)
        "net_vs_hodl_apr": (total - v_hold) / v0 * ann * 100,
        "net_usd_apr": (total - v0) / v0 * ann * 100,
        "hodl_usd_apr": (v_hold - v0) / v0 * ann * 100,
    }
