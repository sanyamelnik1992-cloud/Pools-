"""Текст отчёта по стратегиям: для консоли и Telegram. Главная метрика — SUI-эквивалент (штук SUI)."""
from __future__ import annotations


def _usd(x: float) -> str:
    return f"{'+' if x >= 0 else '−'}${abs(x):,.0f}"


def table(rows: list[dict], staking_apy: float = 0.0) -> str:
    """Подробная таблица для консоли."""
    head = (f"{'Стратегия':28s} {'Диапазон':>13s} {'Сейчас':>7s} {'В диап.':>7s} {'Комиссии':>9s} {'Издержки':>9s} "
            f"{'Пересб.':>7s} {'Стоимость':>10s} {'SUI-экв.':>9s} {'К холду, SUI':>13s} {'К той же доле':>14s}")
    lines = [head, "-" * len(head)]
    for r in rows:
        lo, hi = r["range"]
        now = "в SUI" if r["mode"] == "sui" else "в диап." if r["in_range_now"] else "вне"
        lines.append(f"{r['name'][:28]:28s} {lo:6.4f}–{hi:6.4f} {now:>7s} {r['in_range_pct']:6.0f}% "
                     f"${r['fees_usd']:>8,.0f} ${r['costs_usd']:>8,.0f} {r['rebalances']:>7d} ${r['value']:>9,.0f} "
                     f"{r['value_sui']:>9,.0f} {r['vs_hold_sui_count']:>+13,.0f} {_usd(r['vs_split']):>14s}"
                     + ("  [остановлена]" if r["stopped"] else ""))
    if rows:
        r = rows[0]
        cap = r["capital_sui"]
        stake = cap * (1 + staking_apy * r["days"] / 365)
        lines.append(f"{'просто держать SUI':28s} {'':13s} {'':7s} {'':7s} {'':9s} {'':9s} {'':7s} "
                     f"${cap * r['price']:>9,.0f} {cap:>9,.0f} {0:>+13,.0f}")
        if staking_apy:
            lines.append(f"{'стейкинг SUI':28s} {'':13s} {'':7s} {'':7s} {'':9s} {'':9s} {'':7s} "
                         f"${stake * r['price']:>9,.0f} {stake:>9,.0f} {stake - cap:>+13,.0f}")
        lines.append(f"\nSUI ${r['price']:.4f} · дней {r['days']:.1f} · пропуски (бот не работал) {r['gap_h']:.1f} ч")
        lines.append("SUI-экв. — стоимость позиции в штуках SUI по текущей цене. «К той же доле» — против варианта "
                     "«обменять ту же долю SUI на USDC и держать»: вклад самого пула.")
    return "\n".join(lines)


def short(rows: list[dict]) -> str:
    """Короткий отчёт для Telegram."""
    if not rows:
        return "нет позиций"
    out = [f"SUI ${rows[0]['price']:.4f}, дней {rows[0]['days']:.1f}, старт {rows[0]['capital_sui']:,.0f} SUI"]
    for r in rows:
        out.append(f"• {r['name']}: {r['value_sui']:,.0f} SUI-экв. ({r['vs_hold_sui_count']:+,.0f}), "
                   f"комиссии ${r['fees_usd']:,.0f}, пересборок {r['rebalances']}"
                   + (", сейчас в SUI" if r["mode"] == "sui" else "")
                   + (" [остановлена]" if r["stopped"] else ""))
    return "\n".join(out)
