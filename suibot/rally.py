"""Наблюдение за сильными движениями цены для правил «выйти в SUI на росте» и «выйти в USDC на падении».

Для каждого правила (часов, порог) хранится монотонная очередь цен за последние N часов: минимум окна (для роста)
или максимум (для падения) берётся за O(1), поэтому правила одинаково быстро работают и на годе 5-минутной
истории, и в бумажном режиме.

Глобальный тренд (trend_days): средняя цена за последние N дней по часовым ценам — для фильтра выходов
(выход в SUI только выше средней, в USDC — только ниже). Сброс после возврата в пул её не трогает.

Фаза рынка (Phase): по дневным закрытиям — средняя за N дней и пороги с запасом: confirm закрытий подряд выше
средней на up — «рост», ниже на down — «падение»; между порогами фаза не меняется (не дёргается около средней),
однодневный выброс за порог её тоже не меняет. Решение — раз в сутки, после закрытия дня.
"""
from __future__ import annotations

from collections import deque


CLOSE_S = 2 * 3600       # цена в последние 2 часа дня — его закрытие


class Phase:
    """Фаза рынка по дневным закрытиям: "up" — рост, "down" — падение или боковик, None — мало истории."""

    def __init__(self, days: float, up: float, down: float, kind: str = "sma", confirm: int = 1,
                 state: dict | None = None):
        self.days, self.up, self.down, self.kind, self.confirm = int(days), float(up), float(down), kind, int(confirm)
        st = state or {}
        self.closes = deque((int(d), float(p)) for d, p in st.get("closes") or [])   # (день, цена закрытия)
        cur = st.get("cur")                                                        # (день, последняя цена, когда видна)
        self.cur = (int(cur[0]), float(cur[1]), float(cur[2]) if len(cur) > 2 else None) if cur else None
        self.state = st.get("state")
        self.ema = st.get("ema")
        self.since = st.get("since")                                               # день смены фазы
        self.run = list(st.get("run") or [0, 0])                                   # закрытий подряд выше / ниже порогов

    def feed(self, t: float, p: float):
        """Цена бота. Закрытием дня считается последняя цена дня, если она видна в последние 2 часа дня; если бот
        тогда не работал, день пропускается (его закрытие догружается из предыстории — need_warm)."""
        day = int(t // 86400)
        if self.cur and day > self.cur[0] and (self.cur[2] is None or self.cur[2] >= (self.cur[0] + 1) * 86400 - CLOSE_S):
            self._close(self.cur[0], self.cur[1])
        if not self.cur or day >= self.cur[0]:
            self.cur = (day, p, t)

    def _close(self, day: int, p: float):
        if self.closes and day <= self.closes[-1][0]:
            return
        self.closes.append((day, p))
        while len(self.closes) > max(self.days * 3, self.days + 60):
            self.closes.popleft()
        if self.kind == "ema":
            a = 2 / (self.days + 1)
            self.ema = p if self.ema is None else a * p + (1 - a) * self.ema
        m = self.ma()
        if m is None:
            return
        old = self.state
        self.run = [self.run[0] + 1 if p > m * (1 + self.up) else 0, self.run[1] + 1 if p < m * (1 - self.down) else 0]
        if self.run[0] >= self.confirm:
            self.state = "up"
        elif self.run[1] >= self.confirm:
            self.state = "down"
        elif self.state is None:
            self.state = "up" if p > m else "down"
        if self.state != old:
            self.since = day

    def ma(self) -> float | None:
        """Средняя за days дней по закрытиям; None — закрытий меньше 80% окна."""
        if len(self.closes) < 0.8 * self.days:
            return None
        if self.kind == "ema":
            return self.ema
        a = [p for _, p in list(self.closes)[-self.days:]]
        return sum(a) / len(a)

    def current(self) -> str | None:
        return self.state if self.ma() is not None else None

    def bounds(self) -> tuple[float, float] | None:
        """Цена, выше которой фаза станет «рост», и ниже которой — «падение» (по закрытию дня)."""
        m = self.ma()
        return None if m is None else (m * (1 + self.up), m * (1 - self.down))

    def recompute(self):
        """Пересчитать среднюю и фазу заново по сохранённым закрытиям (после смены настроек или предыстории)."""
        closes = list(self.closes)
        self.closes, self.state, self.ema, self.since, self.run = deque(), None, None, None, [0, 0]
        for d, p in closes:
            self._close(d, p)

    def warm(self, samples: list):
        """Предыстория (часовые цены): последнее значение каждого дня — его закрытие; последний день выборки
        считается незакрытым. Свои закрытия бота важнее; дни, когда бот не работал, берутся из выборки."""
        if not samples:
            return
        days = {}
        for t, p in samples:
            days[int(t // 86400)] = p
        last = max(days)
        open_day = self.cur[0] if self.cur and self.cur[0] > last else last     # дни раньше него закрыты
        merged = {d: p for d, p in days.items() if d < open_day}
        merged.update(dict(self.closes))
        self.closes = deque(sorted(merged.items()))
        self.recompute()
        if self.cur is None or self.cur[0] < last:
            self.cur = (last, days[last], max(t for t, _ in samples))

    def gap(self) -> bool:
        """Нет закрытия вчерашнего дня (бот не работал в конце дня) — нужна предыстория."""
        return bool(self.cur and (not self.closes or self.closes[-1][0] < self.cur[0] - 1))

    def last(self) -> tuple[int, float] | None:
        """Последнее закрытие дня: (день, цена)."""
        return self.closes[-1] if self.closes else None

    def dump(self) -> dict:
        return {"closes": [list(x) for x in self.closes], "cur": list(self.cur) if self.cur else None,
                "state": self.state, "ema": self.ema, "since": self.since, "run": self.run}


class RallyWatch:
    def __init__(self, rules: list | None, state=None, min_step: float = 0.0, drop_rules: list | None = None,
                 trend_days: float | None = None, phase: dict | None = None):
        self.rules = [(float(h), float(r)) for h, r in rules or []]
        self.drop_rules = [(float(h), float(r)) for h, r in drop_rules or []]
        if isinstance(state, list):                       # старый формат: только очереди роста
            state = {"rise": state}
        state = state or {}
        rise, drop = state.get("rise") or [], state.get("drop") or []
        # очереди сохраняются по порядку правил; если правила поменяли — наблюдение начинается заново
        self.q = [deque(tuple(x) for x in qs) for qs in rise] if len(rise) == len(self.rules) else \
            [deque() for _ in self.rules]
        self.dq = [deque(tuple(x) for x in qs) for qs in drop] if len(drop) == len(self.drop_rules) else \
            [deque() for _ in self.drop_rules]
        self.min_step = min_step          # не чаще раза в min_step секунд (бумажный режим опрашивает часто)
        self.last_t = max((q[-1][0] for q in self.q + self.dq if q), default=0.0)
        self.trend_days = float(trend_days or 0.0)
        self.tq = deque(tuple(x) for x in state.get("trend") or []) if self.trend_days else deque()
        self.tsum = sum(p for _, p in self.tq)
        old = state.get("phase") or {}
        self.ph = Phase(state=old, **phase) if phase else None
        if self.ph and (old.get("days"), old.get("kind"), old.get("up"), old.get("down"), old.get("confirm", 1)) != (
                self.ph.days, self.ph.kind, self.ph.up, self.ph.down, self.ph.confirm):
            self.ph.recompute()           # настройки фазы поменяли — фаза пересчитывается по сохранённым закрытиям

    def phase(self) -> str | None:
        """Фаза рынка: "up" — рост, "down" — падение/боковик, None — фазы нет (не задана или мало истории)."""
        return self.ph.current() if self.ph else None

    def feed_trend(self, t: float, p: float):
        """Часовая цена для средней за trend_days (не чаще раза в час)."""
        if not self.trend_days or (self.tq and t - self.tq[-1][0] < 3600):
            return
        self.tq.append((t, p))
        self.tsum += p
        while self.tq[0][0] < t - self.trend_days * 86400:
            self.tsum -= self.tq.popleft()[1]

    def warm_trend(self, samples: list):
        """Предыстория для средней (часовые цены до начала работы): берётся то, что старше уже накопленного."""
        if not self.trend_days or not samples:
            return
        mine = list(self.tq)
        older = [tuple(x) for x in samples if not mine or x[0] < mine[0][0] - 1800]
        self.tq, self.tsum = deque(), 0.0
        for t, p in older + mine:
            self.feed_trend(t, p)

    def warm(self, samples: list):
        """Предыстория (часовые цены Binance) для средней тренда и для фазы рынка."""
        if self.ph:
            self.ph.warm(samples)
        self.warm_trend(samples)

    def need_warm(self) -> bool:
        """Нужна предыстория: средней тренда или фазы ещё нет, или пропущено закрытие дня."""
        return bool(self.trend_days and self.trend_ma() is None) or bool(self.ph and (self.ph.ma() is None or self.ph.gap()))

    def warm_days(self) -> float:
        """Сколько дней предыстории загрузить (для фазы — два окна: чтобы фаза успела установиться)."""
        return max(self.trend_days, 2 * self.ph.days if self.ph else 0)

    def trend_ma(self) -> float | None:
        """Средняя цена за trend_days; None — истории меньше 80% окна (фильтр тогда не действует)."""
        if not self.tq or self.tq[-1][0] - self.tq[0][0] < 0.8 * self.trend_days * 86400:
            return None
        return self.tsum / len(self.tq)

    def trend(self, p: float) -> str | None:
        """"up" — цена выше средней (рынок в целом растёт), "down" — ниже, None — средней пока нет."""
        ma = self.trend_ma()
        return None if ma is None else "up" if p >= ma else "down"

    def add(self, t: float, p: float):
        self.feed_trend(t, p)
        if self.ph:
            self.ph.feed(t, p)
        if t - self.last_t < self.min_step:
            return
        self.last_t = t
        for (h, _), q in zip(self.rules, self.q):          # минимум окна
            while q and q[-1][1] >= p:
                q.pop()
            q.append((t, p))
            while q[0][0] < t - h * 3600:
                q.popleft()
        for (h, _), q in zip(self.drop_rules, self.dq):    # максимум окна
            while q and q[-1][1] <= p:
                q.pop()
            q.append((t, p))
            while q[0][0] < t - h * 3600:
                q.popleft()

    def reset(self):
        """Забыть прошлые цены: движение считается заново от этого момента (после возврата в пул)."""
        for q in self.q + self.dq:
            q.clear()
        self.last_t = 0.0

    def triggered(self, p: float) -> str | None:
        for (h, r), q in zip(self.rules, self.q):
            if q and p / q[0][1] - 1 >= r:
                return f"рост {p / q[0][1] - 1:+.0%} за {h:g} ч"
        return None

    def dropped(self, p: float) -> str | None:
        for (h, r), q in zip(self.drop_rules, self.dq):
            if q and p / q[0][1] - 1 <= -r:
                return f"падение {p / q[0][1] - 1:+.0%} за {h:g} ч"
        return None

    def dump(self) -> dict:
        out = {"rise": [[list(x) for x in q] for q in self.q], "drop": [[list(x) for x in q] for q in self.dq]}
        if self.trend_days:
            out["trend"] = [list(x) for x in self.tq]
        if self.ph:
            out["phase"] = dict(self.ph.dump(), days=self.ph.days, kind=self.ph.kind, up=self.ph.up, down=self.ph.down,
                                confirm=self.ph.confirm)
        return out


def watch_for(s, state=None, min_step: float = 0.0) -> RallyWatch:
    """Наблюдатель для стратегии: правила выходов, фильтр тренда и фаза рынка из её настроек."""
    phase = (dict(days=s.phase_ma_days, up=s.phase_up, down=s.phase_down, kind=s.phase_kind, confirm=s.phase_confirm)
             if getattr(s, "phase_ma_days", None) else None)
    return RallyWatch(s.rally_exit, state, min_step=min_step, drop_rules=s.crash_exit, trend_days=s.trend_ma_days,
                      phase=phase)
