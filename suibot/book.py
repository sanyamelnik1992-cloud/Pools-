"""Учёт виртуальной позиции: открытие, пересборка, начисление комиссий и наград, сравнение с холдом."""
from __future__ import annotations

import time
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
    mode: str = "lp"                # lp — в пуле; sui — вышли в SUI на росте; usdc — в USDC на падении; hold — вручную;
    #                                 up — фаза роста рынка: всё в SUI, пока фаза не сменится
    peak: float = 0.0               # после выхода: максимум цены (в SUI) или минимум (в USDC)
    exits: list = field(default_factory=list)
    resumes: list = field(default_factory=list)
    crashes: list = field(default_factory=list)   # выходы в USDC на падении
    phases: list = field(default_factory=list)    # смены фазы рынка: [время, "up" | "down"]
    # только «тень» и история: стейкинг и лонг на фьючерсах в фазе роста
    perp_sui: float = 0.0           # объём лонга, SUI
    perp_entry: float = 0.0         # цена входа
    perp_margin: float = 0.0        # залог, $ (финансирование списывается из него)
    carry_t: float | None = None    # время последнего начисления стейкинга и финансирования
    staked_sui: float = 0.0         # всего начислено стейкинга, SUI
    funding_usd: float = 0.0        # всего заплачено за удержание лонга, $
    liquidations: list = field(default_factory=list)
    perp_peak: float = 0.0          # максимум цены с открытия лонга (для скользящего стопа и повторного входа)
    perp_stopped: bool = False      # лонг закрыт стопом в этой фазе роста
    stops: list = field(default_factory=list)
    ladder_t: float | None = None   # с какого времени стоит лесенка конца роста (None — обычный пул)

    def sqrt_bounds(self) -> tuple[float, float]:
        if not self.sa:
            self.sa, self.sb = sqrt_of_tick(self.tick_lo), sqrt_of_tick(self.tick_hi)
        return self.sa, self.sb

    def in_range(self, sq: float) -> bool:
        if self.mode != "lp":
            return False
        sa, sb = self.sqrt_bounds()
        return sa <= sq <= sb

    def perp_equity(self, p: float) -> float:
        """Стоимость фьючерса: залог плюс прибыль или убыток, $ (не меньше нуля)."""
        return max(0.0, self.perp_margin + self.perp_sui * (p - self.perp_entry)) if self.perp_sui else 0.0

    def holdings(self, st: dict) -> tuple[float, float]:
        """Монеты a и b: позиция, несобранные комиссии и монеты вне пула (фьючерс — в USDC по его стоимости)."""
        a, b = amounts(self.L, st["sq"], *self.sqrt_bounds()) if self.L else (0.0, 0.0)
        e = self.perp_equity(st["sui"]) * 1e6 if self.perp_sui else 0.0
        if self.a_is_sui:
            return a + self.fees_a + self.idle_a, b + self.fees_b + self.idle_b + e
        return a + self.fees_a + self.idle_a + e, b + self.fees_b + self.idle_b


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
    book.ladder_t = None
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


def bear_range(s: Strategy, p: float) -> tuple[float, float]:
    """Диапазон при конце фазы роста: лесенка только из USDC ниже цены или обычный пул вокруг цены."""
    return s.ladder_range(p) if s.bear_ladder else s.target_range(p)


def phase_text(watch, side: str) -> str:
    """Почему фаза такая: последнее дневное закрытие против средней, порог и сколько закрытий подряд за ним."""
    ph = watch.ph
    _, c = ph.last()
    m = ph.ma()
    n = ph.run[0 if side == "up" else 1]
    note = (f"закрытий подряд за порогом: {n}" if n >= ph.confirm else
            "фаза держится с " + time.strftime("%d.%m", time.gmtime(ph.since * 86400)) if ph.since is not None else "")
    if side == "up":
        return (f"закрытие дня ${c:.4f} {'выше' if c >= m else 'ниже'} средней за {ph.days} дн. (${m:.4f}) на "
                f"{abs(c / m - 1):.0%} (порог +{ph.up:.1%}; {note}) — фаза роста")
    return (f"закрытие дня ${c:.4f} {'ниже' if c <= m else 'выше'} средней за {ph.days} дн. (${m:.4f}) на "
            f"{abs(1 - c / m):.0%} (порог −{ph.down:.1%}; {note}) — фаза падения или боковика")


MAINT = 0.05          # фьючерс ликвидируется, когда его стоимость ниже 5% объёма (с запасом к бирже)


def enter_up(book: Book, st: dict, costs: Costs, s: Strategy | None = None) -> float:
    """Фаза роста: снять позицию и всё в SUI (после выхода в SUI на росте менять уже нечего); у стратегии с плечом
    (только «тень») — ещё лонг на фьючерсах."""
    cost = 0.0 if book.mode == "sui" and not book.L else close_to_idle(book, st, costs, "sui")
    book.mode = "up"
    book.phases.append([st["t"], "up"])
    book.perp_stopped = False
    book.ladder_t = None
    if s is not None and s.up_leverage > 1 and s.perp_leverage > 1:
        cost += open_perp(book, s, st, costs)
    return cost


def open_perp(book: Book, s: Strategy, st: dict, costs: Costs) -> float:
    """Лонг на (up_leverage − 1) × капитал: часть SUI меняется на USDC-залог, объём = залог × perp_leverage.
    Вместе с оставшимися SUI получается up_leverage × капитал в SUI."""
    p = st["sui"]
    a, b = book.holdings(st)
    equity = a * st["ua"] + b * st["ub"]
    margin = (s.up_leverage - 1) * equity / (s.perp_leverage - 1)
    usdc = (book.idle_b if book.a_is_sui else book.idle_a) / 1e6       # USDC вне пула (например, после стопа)
    need = margin - usdc                                               # > 0 — продать SUI на залог, < 0 — купить SUI
    sui_raw = need / p * 1e9
    if book.a_is_sui:
        book.idle_a -= sui_raw
        book.idle_b = 0.0
    else:
        book.idle_b -= sui_raw
        book.idle_a = 0.0
    notional = margin * s.perp_leverage
    cost = abs(need) * (costs.swap_fee + costs.slippage) + notional * costs.swap_fee + costs.gas_sui * p
    book.perp_margin, book.perp_sui, book.perp_entry, book.perp_peak = margin - cost, notional / p, p, p
    book.costs_usd += cost
    return cost


def close_perp(book: Book, st: dict, costs: Costs) -> float:
    """Закрыть лонг: его стоимость — в USDC вне пула."""
    if not book.perp_sui:
        return 0.0
    p = st["sui"]
    cost = book.perp_sui * p * costs.swap_fee + costs.gas_sui * p
    usd = max(0.0, book.perp_equity(p) - cost)
    if book.a_is_sui:
        book.idle_b += usd * 1e6
    else:
        book.idle_a += usd * 1e6
    book.perp_sui = book.perp_margin = book.perp_entry = 0.0
    book.costs_usd += cost
    return cost


def carry(book: Book, s: Strategy, st: dict, costs: Costs | None = None) -> tuple[str, str] | None:
    """В фазе роста: стейкинг лежащих SUI, плата за удержание лонга, стоп-лосс, ликвидация и повторный вход."""
    t, p = st["t"], st["sui"]
    dt = t - book.carry_t if book.carry_t is not None else 0.0
    book.carry_t = t
    if book.mode != "up" or dt <= 0:
        return None
    yr = dt / (365 * 86400)
    if s.stake_apy:
        add = (book.idle_a if book.a_is_sui else book.idle_b) * s.stake_apy * yr
        if book.a_is_sui:
            book.idle_a += add
        else:
            book.idle_b += add
        book.staked_sui += add / 1e9
        book.fees_usd += add / 1e9 * p
    if book.perp_sui:
        fee = book.perp_sui * p * s.funding_apy * yr
        book.perp_margin -= fee
        book.funding_usd += fee
        book.costs_usd += fee
        book.perp_peak = max(book.perp_peak, p)
        ref = book.perp_peak if s.perp_trail else book.perp_entry
        if s.perp_stop and p <= ref * (1 - s.perp_stop) and costs is not None:
            pnl = book.perp_sui * (p - book.perp_entry)
            close_perp(book, st, costs)
            book.perp_stopped = True
            book.stops.append(t)
            return "стоп-лосс", (f"цена ${p:.4f} на {1 - p / ref:.0%} ниже {'максимума' if s.perp_trail else 'входа'} "
                                 f"${ref:.4f}: лонг закрыт, {'прибыль' if pnl >= 0 else 'убыток'} ≈${abs(pnl):,.0f}")
        if book.perp_margin + book.perp_sui * (p - book.perp_entry) <= MAINT * book.perp_sui * p:
            lost = book.perp_margin
            book.perp_sui = book.perp_margin = book.perp_entry = 0.0
            book.liquidations.append(t)
            return "ликвидация", f"цена ${p:.4f}: лонг на фьючерсах ликвидирован, потерян залог ≈${lost:,.0f}"
    elif (book.perp_stopped and s.perp_reenter and costs is not None and s.up_leverage > 1 and s.perp_leverage > 1
          and p > book.perp_peak):
        book.perp_stopped = False
        open_perp(book, s, st, costs)
        return "лонг снова открыт", f"цена ${p:.4f} обновила максимум — рост продолжается"
    return None


def decide(book: Book, s: Strategy, st: dict, watch) -> tuple[str, str] | None:
    """Решение по правилам стратегии (без исполнения): ("exit" | "crash" | "resume" | "rebalance", причина) или None;
    ("skip_exit" | "skip_crash", причина) — выход пропущен фильтром тренда (сообщить, но ничего не делать);
    ("bull", причина) — началась фаза роста: всё в SUI; ("bear", причина) — фаза роста кончилась: снова в пул.
    Обновляет наблюдение за ростом, пик после выхода и время выхода цены из диапазона."""
    p, t = st["sui"], st["t"]
    watch.add(t, p)
    phase = watch.phase() if s.phase_ma_days else None
    if book.mode == "up":
        if not s.phase_ma_days:                    # стратегию сменили на вариант без фаз — снова в пул
            return "bear", "в стратегии нет фазы рынка (phase_ma_days) — бот снова в пуле"
        return ("bear", phase_text(watch, "down")) if phase == "down" else None
    if phase == "up" and book.mode in ("lp", "sui", "usdc"):
        return "bull", phase_text(watch, "up")
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
    if book.ladder_t is not None:                  # лесенка конца роста: ждёт падения, выходов и пересборки нет
        lo, _ = book.range_usd
        if s.ladder_days and t - book.ladder_t >= s.ladder_days * 86400:
            return "rebalance", f"лесенка ждала падения {s.ladder_days:g} дн. — дальше обычный пул"
        if p < lo:
            update_out(book, st)
            if t - book.out_since >= s.out_minutes * 60:
                return "rebalance", f"лесенка выкуплена (цена ниже ${lo:.4f}) — дальше обычный пул"
        else:
            book.out_since = None
        return None
    trend = watch.trend(p) if s.trend_ma_days else None
    skip = None
    why = watch.triggered(p) if s.rally_exit else None
    if why:
        if trend != "down":
            return "exit", why
        skip = ("skip_exit", f"{why}, но цена ниже средней за {s.trend_ma_days:g} дн. "
                             f"(${watch.trend_ma():.4f}) — похоже на отскок на падающем рынке")
    why = watch.dropped(p) if s.crash_exit else None
    if why:
        if trend != "up":
            return "crash", why
        skip = skip or ("skip_crash", f"{why}, но цена выше средней за {s.trend_ma_days:g} дн. "
                                      f"(${watch.trend_ma():.4f}) — похоже на провал на растущем рынке")
    update_out(book, st)
    if book.out_since is not None:
        reason = s.rebalance_reason(book, p, t)
        if reason:
            return "rebalance", reason
    return skip


def step(book: Book, s: Strategy, st: dict, watch, costs: Costs) -> tuple[str, str] | None:
    """Решение и его виртуальное исполнение (бумага, история, сценарии). Возвращает (событие, описание)."""
    ev = carry(book, s, st, costs)
    if ev:
        return ev
    d = decide(book, s, st, watch)
    if d is None or d[0].startswith("skip"):
        return None
    kind, why = d
    p, t = st["sui"], st["t"]
    if kind == "bull":
        cost = enter_up(book, st, costs, s)
        sui = (book.idle_a if book.a_is_sui else book.idle_b) / 1e9
        lev = f", лонг {book.perp_sui:,.0f} SUI на фьючерсах" if book.perp_sui else ""
        return "фаза роста", f"{why}: всё в SUI — {sui:,.0f} SUI{lev}, издержки ${cost:.2f}"
    if kind == "bear":
        close_perp(book, st, costs)
        a, b = book.holdings(st)
        cost = open_position(book, st, *bear_range(s, p), a, b, costs)
        book.phases.append([t, "down"])
        book.ladder_t = t if s.bear_ladder else None
        watch.reset()
        lo, hi = book.range_usd
        what = (f"всё в USDC лесенкой {lo:.4f}–{hi:.4f}: по пути вниз пул купит SUI" if s.bear_ladder
                else f"снова в пул, диапазон {lo:.4f}–{hi:.4f}")
        return "конец фазы роста", f"{why}: {what}, издержки ${cost:.2f}"
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
    if book.ladder_t is not None:                 # лесенка отработала падение — выходы считаются заново от этой цены
        watch.reset()
        watch.add(t, p)
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
