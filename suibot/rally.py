"""Наблюдение за ростом цены для правила «выйти в SUI на сильном росте».

Для каждого правила (часов, рост) хранится монотонная очередь цен за последние N часов: минимум окна
берётся за O(1), поэтому правило одинаково быстро работает и на годе 5-минутной истории, и в бумажном режиме.
"""
from __future__ import annotations

from collections import deque


class RallyWatch:
    def __init__(self, rules: list | None, state: list | None = None, min_step: float = 0.0):
        self.rules = [(float(h), float(r)) for h, r in rules or []]
        self.q = [deque(tuple(x) for x in qs) for qs in state] if state else [deque() for _ in self.rules]
        self.min_step = min_step          # не чаще раза в min_step секунд (бумажный режим опрашивает часто)
        self.last_t = max((q[-1][0] for q in self.q if q), default=0.0)

    def add(self, t: float, p: float):
        if t - self.last_t < self.min_step:
            return
        self.last_t = t
        for (h, _), q in zip(self.rules, self.q):
            while q and q[-1][1] >= p:
                q.pop()
            q.append((t, p))
            while q[0][0] < t - h * 3600:
                q.popleft()

    def reset(self):
        """Забыть прошлые цены: рост считается заново от этого момента (после возврата в пул)."""
        for q in self.q:
            q.clear()
        self.last_t = 0.0

    def triggered(self, p: float) -> str | None:
        for (h, r), q in zip(self.rules, self.q):
            if q and p / q[0][1] - 1 >= r:
                return f"рост {p / q[0][1] - 1:+.0%} за {h:g} ч"
        return None

    def dump(self) -> list:
        return [[list(x) for x in q] for q in self.q]
