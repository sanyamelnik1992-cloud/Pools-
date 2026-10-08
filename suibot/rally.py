"""Наблюдение за сильными движениями цены для правил «выйти в SUI на росте» и «выйти в USDC на падении».

Для каждого правила (часов, порог) хранится монотонная очередь цен за последние N часов: минимум окна (для роста)
или максимум (для падения) берётся за O(1), поэтому правила одинаково быстро работают и на годе 5-минутной
истории, и в бумажном режиме.

Глобальный тренд (trend_days): средняя цена за последние N дней по часовым ценам — для фильтра выходов
(выход в SUI только выше средней, в USDC — только ниже). Сброс после возврата в пул её не трогает.
"""
from __future__ import annotations

from collections import deque


class RallyWatch:
    def __init__(self, rules: list | None, state=None, min_step: float = 0.0, drop_rules: list | None = None,
                 trend_days: float | None = None):
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
        return out
