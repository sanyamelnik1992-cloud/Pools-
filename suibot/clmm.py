"""Математика концентрированной ликвидности в «сырых» единицах пула.

Сырая цена P — сколько минимальных единиц монеты b дают за минимальную единицу монеты a, sp = √P.
Ликвидность L — в тех же единицах, что и поле liquidity объекта пула, поэтому к ней напрямую применим
прирост счётчиков fee_growth_global и наград (Q64.64 на единицу ликвидности).
"""
from __future__ import annotations

import math

Q64 = 2 ** 64
U128 = 2 ** 128
_LOG_BASE = math.log(1.0001)


def tick_of(p_raw: float) -> float:
    return math.log(p_raw) / _LOG_BASE


def sqrt_of_tick(tick: int) -> float:
    return math.exp(tick * _LOG_BASE / 2)


def snap_ticks(p_lo: float, p_hi: float, spacing: int) -> tuple[int, int]:
    """Границы на ближайших тиках сетки пула (как в интерфейсе Cetus), диапазон не меньше одного шага."""
    lo = round(tick_of(min(p_lo, p_hi)) / spacing) * spacing
    hi = round(tick_of(max(p_lo, p_hi)) / spacing) * spacing
    return lo, max(hi, lo + spacing)


def amounts(L: float, sp: float, sa: float, sb: float) -> tuple[float, float]:
    """Монеты a и b (мин. единицы) ликвидности L в диапазоне [sa², sb²] при цене sp²."""
    if sp <= sa:
        return L * (1 / sa - 1 / sb), 0.0
    if sp >= sb:
        return 0.0, L * (sb - sa)
    return L * (1 / sp - 1 / sb), L * (sp - sa)


def growth_delta(new: int, old: int) -> int:
    """Прирост счётчика u128 с учётом переполнения."""
    return (int(new) - int(old)) % U128
