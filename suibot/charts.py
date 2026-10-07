"""Недельный график для Telegram: цена SUI и диапазон бота, штуки SUI у бота против «держать SUI»."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path


def weekly_chart(rows: list[dict], path: Path) -> Path | None:
    """rows — строки snapshots.csv боевого бота. None — нет matplotlib или мало данных."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.dates as mdates
        import matplotlib.pyplot as plt
    except ImportError:
        return None
    rows = [r for r in rows if r.get("price")]
    if len(rows) < 2:
        return None
    t = [datetime.fromtimestamp(float(r["t"])) for r in rows]
    price = [float(r["price"]) for r in rows]
    lo = [float(r["lo"]) if r.get("lo") else float("nan") for r in rows]
    hi = [float(r["hi"]) if r.get("hi") else float("nan") for r in rows]
    sui = [float(r["value_sui"]) for r in rows]
    hold = [float(r["capital_sui"]) for r in rows]
    fig, (a1, a2) = plt.subplots(2, 1, figsize=(8, 6), sharex=True, gridspec_kw={"height_ratios": [3, 2]})
    a1.fill_between(t, lo, hi, step="post", color="#4C9AFF", alpha=0.18, label="диапазон бота")
    a1.plot(t, price, color="#1F2937", lw=1.4, label="цена SUI, $")
    a1.set_ylabel("$ за SUI")
    a1.legend(loc="upper left", fontsize=8, frameon=False)
    a1.grid(alpha=0.25)
    a2.plot(t, sui, color="#16A34A", lw=1.6, label="у бота, SUI-экв.")
    a2.plot(t, hold, color="#9CA3AF", lw=1.2, ls="--", label="держать SUI")
    a2.set_ylabel("штук SUI")
    a2.legend(loc="upper left", fontsize=8, frameon=False)
    a2.grid(alpha=0.25)
    a2.xaxis.set_major_formatter(mdates.DateFormatter("%d.%m"))
    fig.suptitle("Бот SUI/USDC за неделю", fontsize=11)
    fig.tight_layout()
    fig.savefig(path, dpi=130)
    plt.close(fig)
    return path
