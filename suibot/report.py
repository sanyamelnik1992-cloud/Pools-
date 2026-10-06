"""Текст отчёта по стратегиям: для консоли и Telegram."""
from __future__ import annotations


def _usd(x: float) -> str:
    return f"{'+' if x >= 0 else '−'}${abs(x):,.0f}"


def table(rows: list[dict]) -> str:
    """Подробная таблица для консоли."""
    head = (f"{'Стратегия':28s} {'Диапазон':>13s} {'Сейчас':>7s} {'В диап.':>7s} {'Стоимость':>10s} {'Комиссии':>9s} "
            f"{'Издержки':>9s} {'Пересб.':>7s} {'К холду SUI':>12s} {'К той же доле':>14s}")
    lines = [head, "-" * len(head)]
    for r in rows:
        lo, hi = r["range"]
        now = "в диап." if r["in_range_now"] else "вне"
        lines.append(f"{r['name'][:28]:28s} {lo:6.4f}–{hi:6.4f} {now:>7s} {r['in_range_pct']:6.0f}% "
                     f"${r['value']:>9,.0f} ${r['fees_usd']:>8,.0f} ${r['costs_usd']:>8,.0f} {r['rebalances']:>7d} "
                     f"{_usd(r['vs_sui']):>12s} {_usd(r['vs_split']):>14s}" + ("  [остановлена]" if r["stopped"] else ""))
    if rows:
        r = rows[0]
        lines.append(f"\nSUI ${r['price']:.4f} · дней работы {r['days']:.1f} · пропуски (бот не работал) {r['gap_h']:.1f} ч")
        lines.append("«К той же доле» — против варианта «обменять ту же долю SUI на USDC и просто держать»: это и есть "
                     "вклад самого пула (комиссии минус потери от движения цены и пересборок).")
    return "\n".join(lines)


def short(rows: list[dict]) -> str:
    """Короткий отчёт для Telegram."""
    if not rows:
        return "нет позиций"
    out = [f"SUI ${rows[0]['price']:.4f}, дней {rows[0]['days']:.1f}"]
    for r in rows:
        out.append(f"• {r['name']}: ${r['value']:,.0f}, комиссии ${r['fees_usd']:,.0f}, пересборок {r['rebalances']}, "
                   f"к холду SUI {_usd(r['vs_sui'])}, к той же доле {_usd(r['vs_split'])}"
                   + (" [остановлена]" if r["stopped"] else ""))
    return "\n".join(out)
