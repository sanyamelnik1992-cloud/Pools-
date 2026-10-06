"""Учёт виртуальной позиции: открытие, пересборка, начисление комиссий и наград, сравнение с холдом."""
from __future__ import annotations

from dataclasses import dataclass, field

from suibot.chain import PoolCfg, raw_to_usd, usd_to_raw
from suibot.clmm import Q64, amounts, growth_delta, snap_ticks, sqrt_of_tick
from suibot.strategy import Strategy


@dataclass
class Costs:
    swap_fee: float = 0.0005   # комиссия маршрута при обмене
    slippage: float = 0.0005
    gas_sui: float = 0.02      # газ на одну пересборку (снять, обменять, добавить)


@dataclass
class Book:
    name: str
    pool: str
    a_is_sui: bool
    L: float = 0.0                  # ликвидность в сырых единицах пула
    tick_lo: int = 0
    tick_hi: int = 0
    range_usd: list = field(default_factory=lambda: [0.0, 0.0])
    fees_a: float = 0.0             # несобранные комиссии, мин. единицы монет a и b
    fees_b: float = 0.0
    rewards: dict = field(default_factory=dict)   # тип токена награды → мин. единицы
    fees_usd: float = 0.0           # всего начислено комиссий и наград, $ по ценам начисления
    costs_usd: float = 0.0          # обмены и газ
    rebalances: list = field(default_factory=list)
    out_since: float | None = None
    in_range_s: float = 0.0
    total_s: float = 0.0
    gap_s: float = 0.0              # время, когда бот не работал
    start: dict = field(default_factory=dict)
    stopped: bool = False
    sa: float = 0.0                 # границы в √сырой цены (кэш, пересчитываются из тиков)
    sb: float = 0.0
    idle_a: float = 0.0             # монеты вне пула (после выхода в SUI), мин. единицы
    idle_b: float = 0.0
    mode: str = "lp"                # lp — в пуле; sui — вышли в SUI на росте; usdc — в USDC на падении; hold — вручную
    peak: float = 0.0               # после выхода: максимум цены (в SUI) или минимум (в USDC)
    exits: list = field(default_factory=list)
    resumes: list = field(default_factory=list)
    crashes: list = field(default_factory=list)   # выходы в USDC на падении

    def sqrt_bounds(self) -> tuple[float, float]:
        if not self.sa:
            self.sa, self.sb = sqrt_of_tick(self.tick_lo), sqrt_of_tick(self.tick_hi)
        return self.sa, self.sb

    def in_range(self, sq: float) -> bool:
        if self.mode != "lp":
            return False
        sa, sb = self.sqrt_bounds()
        return sa <= sq <= sb

    def holdings(self, st: dict) -> tuple[float, float]:
        """Монеты a и b: позиция, несобранные комиссии и монеты вне пула."""
        a, b = amounts(self.L, st["sq"], *self.sqrt_bounds()) if self.L else (0.0, 0.0)
        return a + self.fees_a + self.idle_a, b + self.fees_b + self.idle_b


def open_position(book: Book, st: dict, lo_usd: float, hi_usd: float, a: float, b: float, costs: Costs) -> float:
    """Собрать позицию в диапазоне lo–hi ($ за SUI) из монет a и b; вернуть издержки в $."""
    book.tick_lo, book.tick_hi = snap_ticks(usd_to_raw(lo_usd, book.a_is_sui), usd_to_raw(hi_usd, book.a_is_sui),
                                            st["spacing"])
    book.sa = 0.0
    sa, sb = book.sqrt_bounds()
    a1, b1 = amounts(1.0, st["sq"], sa, sb)
    per_l = a1 * st["ua"] + b1 * st["ub"]
    value = a * st["ua"] + b * st["ub"]
    swap = abs(a * st["ua"] - value * a1 * st["ua"] / per_l)   # сколько $ нужно обменять до нужной пропорции
    cost = swap * (costs.swap_fee + costs.slippage) + costs.gas_sui * st["sui"]
    book.L = (value - cost) / per_l
    book.range_usd = sorted([raw_to_usd(sa * sa, book.a_is_sui), raw_to_usd(sb * sb, book.a_is_sui)])
    book.fees_a = book.fees_b = 0.0                       # комиссии реинвестируются
    book.idle_a = book.idle_b = 0.0
    book.mode = "lp"
    book.costs_usd += cost
    return cost


def init_book(s: Strategy, pc: PoolCfg, st: dict, costs: Costs, scale: float = 1.0) -> Book:
    """Открыть позицию из capital_sui; запомнить, что было бы при холде SUI и при холде той же доли."""
    book = Book(s.name, s.pool, pc.a_is_sui)
    raw = s.capital_sui * 1e9
    a, b = (raw, 0.0) if pc.a_is_sui else (0.0, raw)
    open_position(book, st, *s.target_range(st["sui"], first=True, scale=scale), a, b, costs)
    pa, pb = amounts(book.L, st["sq"], *book.sqrt_bounds())
    sui, usdc = (pa / 1e9, pb / 1e6) if pc.a_is_sui else (pb / 1e9, pa / 1e6)
    book.start = {"t": st["t"], "price": st["sui"], "capital_sui": s.capital_sui, "split_sui": sui, "split_usdc": usdc}
    return book


def rebalance(book: Book, st: dict, s: Strategy, costs: Costs) -> float:
    """Снять позицию вместе с комиссиями и собрать заново вокруг текущей цены."""
    a, b = book.holdings(st)
    cost = open_position(book, st, *s.target_range(st["sui"]), a, b, costs)
    book.rebalances.append(st["t"])
    book.out_since = None
    return cost


def _time(book: Book, k: float, dt: float):
    book.total_s += dt
    book.in_range_s += k * dt


def accrue_growth(book: Book, prev: dict, cur: dict, price_of) -> float:
    """Бумажный режим: начислить по приросту счётчиков пула (ровно то, что получила бы реальная ликвидность).
    Доля интервала в диапазоне оценивается по двум замерам. Возвращает начисленное в $."""
    k = (book.in_range(prev["sq"]) + book.in_range(cur["sq"])) / 2
    _time(book, k, cur["t"] - prev["t"])
    if not k or not book.L:
        return 0.0
    da = k * growth_delta(cur["fa"], prev["fa"]) * book.L / Q64
    db = k * growth_delta(cur["fb"], prev["fb"]) * book.L / Q64
    book.fees_a += da
    book.fees_b += db
    usd = da * cur["ua"] + db * cur["ub"]
    for t, g in cur["rew"].items():
        if t not in prev["rew"]:
            continue
        amt = k * growth_delta(g, prev["rew"][t]) * book.L / Q64
        if amt:
            book.rewards[t] = book.rewards.get(t, 0.0) + amt
            px, dec = price_of(t)
            usd += amt * px / 10 ** dec
    book.fees_usd += usd
    return usd


def accrue_usd(book: Book, k: float, usd: float, st: dict, dt: float):
    """Проверка на истории: доход в $ зачисляется на сторону USDC."""
    _time(book, k, dt)
    if usd:
        if book.a_is_sui:
            book.fees_b += usd / st["ub"]
        else:
            book.fees_a += usd / st["ua"]
        book.fees_usd += usd


def close_to_idle(book: Book, st: dict, costs: Costs, to: str | None = None) -> float:
    """Снять позицию вместе с комиссиями; to="sui" / "usdc" — обменять всё в одну монету. Возвращает издержки в $."""
    a, b = book.holdings(st)
    sui_raw, usdc_raw = (a, b) if book.a_is_sui else (b, a)
    cost = costs.gas_sui * st["sui"]
    if to == "sui" and usdc_raw:
        usdc = usdc_raw * 1e-6
        cost += usdc * (costs.swap_fee + costs.slippage)
        sui_raw, usdc_raw = sui_raw + (usdc - cost) / st["sui"] * 1e9, 0.0
    elif to == "usdc" and sui_raw:
        usd = sui_raw / 1e9 * st["sui"]
        cost += usd * (costs.swap_fee + costs.slippage)
        sui_raw, usdc_raw = 0.0, usdc_raw + (usd - cost) * 1e6
    else:
        sui_raw -= costs.gas_sui * 1e9
    book.L = book.fees_a = book.fees_b = 0.0
    book.idle_a, book.idle_b = (sui_raw, usdc_raw) if book.a_is_sui else (usdc_raw, sui_raw)
    book.out_since = None
    book.costs_usd += cost
    return cost


def exit_to_sui(book: Book, st: dict, costs: Costs) -> float:
    """Выйти из пула: снять позицию с комиссиями и обменять весь USDC на SUI. Возвращает издержки в $."""
    cost = close_to_idle(book, st, costs, "sui")
    book.mode, book.peak = "sui", st["sui"]
    book.exits.append(st["t"])
    return cost


def exit_to_usdc(book: Book, st: dict, costs: Costs) -> float:
    """Выйти из пула на падении: снять позицию с комиссиями и обменять все SUI на USDC."""
    cost = close_to_idle(book, st, costs, "usdc")
    book.mode, book.peak = "usdc", st["sui"]
    book.crashes.append(st["t"])
    return cost


def decide(book: Book, s: Strategy, st: dict, watch) -> tuple[str, str] | None:
    """Решение по правилам стратегии (без исполнения): ("exit" | "resume" | "rebalance", причина) или None.
    Обновляет наблюдение за ростом, пик после выхода и время выхода цены из диапазона."""
    p, t = st["sui"], st["t"]
    watch.add(t, p)
    if book.mode == "sui":
        book.peak = max(book.peak, p)
        if s.resume_drop_pct is not None and p <= book.peak * (1 - s.resume_drop_pct):
            return "resume", f"цена на {1 - p / book.peak:.0%} ниже пика {book.peak:.4f}"
        return None
    if book.mode == "usdc":
        book.peak = min(book.peak, p)
        if s.resume_rise_pct is not None and p >= book.peak * (1 + s.resume_rise_pct):
            return "resume", f"цена на {p / book.peak - 1:.0%} выше минимума {book.peak:.4f}"
        return None
    if book.mode != "lp":
        return None
    why = watch.triggered(p) if s.rally_exit else None
    if why:
        return "exit", why
    why = watch.dropped(p) if s.crash_exit else None
    if why:
        return "crash", why
    update_out(book, st)
    if book.out_since is not None:
        reason = s.rebalance_reason(book, p, t)
        if reason:
            return "rebalance", reason
    return None


def step(book: Book, s: Strategy, st: dict, watch, costs: Costs) -> tuple[str, str] | None:
    """Решение и его виртуальное исполнение (бумага, история, сценарии). Возвращает (событие, описание)."""
    d = decide(book, s, st, watch)
    if d is None:
        return None
    kind, why = d
    p, t = st["sui"], st["t"]
    if kind == "resume":
        a, b = book.holdings(st)
        cost = open_position(book, st, *s.target_range(p), a, b, costs)
        book.resumes.append(t)
        watch.reset()
        lo, hi = book.range_usd
        return "возврат в пул", f"{why}: диапазон {lo:.4f}–{hi:.4f}, издержки ${cost:.2f}"
    if kind == "exit":
        cost = exit_to_sui(book, st, costs)
        sui = (book.idle_a if book.a_is_sui else book.idle_b) / 1e9
        return "выход в SUI", (f"{why}: всё в SUI — {sui:,.0f} SUI, издержки ${cost:.2f}"
                               + ("" if s.resume_drop_pct is not None else ", бот больше не работает"))
    if kind == "crash":
        cost = exit_to_usdc(book, st, costs)
        usdc = (book.idle_b if book.a_is_sui else book.idle_a) / 1e6
        return "выход в USDC", (f"{why}: всё в USDC — ${usdc:,.0f}, издержки ${cost:.2f}"
                                + ("" if s.resume_rise_pct is not None else ", бот больше не работает"))
    old = book.range_usd
    cost = rebalance(book, st, s, costs)
    lo, hi = book.range_usd
    return "пересборка", (f"{why}: {old[0]:.4f}–{old[1]:.4f} → {lo:.4f}–{hi:.4f}, издержки ${cost:.2f}, "
                          f"всего пересборок {len(book.rebalances)}")


def update_out(book: Book, st: dict):
    if book.in_range(st["sq"]):
        book.out_since = None
    elif book.out_since is None:
        book.out_since = st["t"]


def summary(book: Book, st: dict, price_of) -> dict:
    a, b = book.holdings(st)
    rew = sum(amt * px / 10 ** dec for t, amt in book.rewards.items() for px, dec in [price_of(t)])
    value = a * st["ua"] + b * st["ub"] + rew
    p, s0 = st["sui"], book.start
    hold_sui = s0["capital_sui"] * p
    hold_split = s0["split_sui"] * p + s0["split_usdc"]
    sui_part = (a * st["ua"]) if book.a_is_sui else (b * st["ub"])
    pos = a * st["ua"] + b * st["ub"]
    return {"name": book.name, "price": p, "value": value, "hold_sui": hold_sui, "hold_split": hold_split,
            "vs_sui": value - hold_sui, "vs_split": value - hold_split, "fees_usd": book.fees_usd,
            "capital_sui": s0["capital_sui"], "value_sui": value / p, "vs_hold_sui_count": value / p - s0["capital_sui"],
            "costs_usd": book.costs_usd, "rebalances": len(book.rebalances), "range": book.range_usd,
            "in_range_now": book.in_range(st["sq"]), "sui_share": sui_part / pos * 100 if pos else 0.0,
            "in_range_pct": book.in_range_s / book.total_s * 100 if book.total_s else 100.0,
            "days": (st["t"] - s0["t"]) / 86400, "gap_h": book.gap_s / 3600, "stopped": book.stopped,
            "mode": book.mode, "exits": len(book.exits), "resumes": len(book.resumes), "crashes": len(book.crashes),
            "sui_amount": sui_part / p, "usdc_amount": value - sui_part,
            "split_sui": s0["split_sui"], "split_usdc": s0["split_usdc"]}


def check_stop(book: Book, s: Strategy, summ: dict) -> bool:
    """Правило остановки: пул отстал от «держать ту же долю» больше чем на stop_vs_split_pct %."""
    if book.stopped or s.stop_vs_split_pct is None:
        return False
    if summ["vs_split"] < -s.stop_vs_split_pct / 100 * summ["hold_split"]:
        book.stopped = True
        return True
    return False
