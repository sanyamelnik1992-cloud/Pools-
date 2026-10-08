"""Правила стратегии: какой диапазон открывать и когда пересобирать. Одни и те же для бумаги и истории."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Strategy:
    name: str
    pool: str
    capital_sui: float                 # стартовый капитал в SUI; нужная доля меняется на USDC при открытии
    range_down: float = 0.05           # нижняя граница: цена × (1 − range_down)
    range_up: float = 0.05             # верхняя граница: цена × (1 + range_up)
    initial_range: list[float] | None = None   # первая позиция в $ за SUI, например [1.20, 1.30]
    rebalance: str = "both"            # both — в обе стороны, down — только вниз (выше остаёмся в USDC),
                                       # up — только вверх, none — никогда
    out_minutes: float = 0.0           # сколько цена должна пробыть вне диапазона до пересборки
    cooldown_minutes: float = 0.0      # пауза после пересборки
    max_per_day: int = 48              # не больше пересборок за сутки
    stop_vs_split_pct: float | None = None   # прекратить пересборки, если пул отстал от «держать ту же долю» на N%
    rally_exit: list | None = None     # выйти в SUI на сильном росте: [[часов, рост], ...], например [[24, 0.10]] —
                                       # цена выросла на 10% от минимума за последние 24 ч
    resume_drop_pct: float | None = None   # вернуться в пул, когда цена упадёт на столько от пика после выхода;
                                           # не задано — после выхода бот больше не работает (держит SUI)
    crash_exit: list | None = None     # выйти в USDC на сильном падении: [[часов, падение], ...], например [[72, 0.15]] —
                                       # цена упала на 15% от максимума за последние 72 ч
    resume_rise_pct: float | None = None   # вернуться в пул, когда цена вырастет на столько от минимума после выхода
                                           # в USDC; не задано — бот остаётся в USDC
    phase_ma_days: float | None = None # фаза рынка по дневным закрытиям: средняя за N дней; «рост» — держать 100% SUI,
                                       # «падение/боковик» — пул по правилам выше; не задано — без фаз
    phase_up: float = 0.05             # фаза «рост» — закрытия выше средней на столько
    phase_down: float = 0.05           # фаза «падение» — закрытия ниже средней на столько (между — без смены)
    phase_confirm: int = 2             # сколько закрытий дня подряд за порогом нужно для смены фазы
    phase_kind: str = "sma"            # sma — простая средняя, ema — экспоненциальная
    trend_ma_days: float | None = None # фильтр глобального тренда: выход в SUI — только если цена выше средней за
                                       # N дней, в USDC — только если ниже (отскоки и провалы против тренда
                                       # бот пережидает в пуле); не задано — выходы без фильтра

    def target_range(self, price: float, first: bool = False, scale: float = 1.0) -> tuple[float, float]:
        """Диапазон в $ за SUI. scale — пересчёт initial_range к другой стартовой цене (проверка на истории)."""
        if first and self.initial_range:
            return self.initial_range[0] * scale, self.initial_range[1] * scale
        return price * (1 - self.range_down), price * (1 + self.range_up)

    def rebalance_reason(self, book, price: float, now: float) -> str | None:
        """Причина пересборки или None."""
        if self.rebalance == "none" or book.stopped or book.mode != "lp" or book.out_since is None:
            return None
        lo, hi = book.range_usd
        side = "down" if price < lo else "up" if price > hi else None
        if side is None or self.rebalance not in ("both", side):
            return None
        if now - book.out_since < self.out_minutes * 60:
            return None
        if book.rebalances and now - book.rebalances[-1] < self.cooldown_minutes * 60:
            return None
        if sum(1 for t in book.rebalances if now - t < 86400) >= self.max_per_day:
            return None
        return "цена ниже диапазона" if side == "down" else "цена выше диапазона"
