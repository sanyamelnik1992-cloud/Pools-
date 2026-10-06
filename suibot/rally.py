"""Наблюдение за сильными движениями цены для правил «выйти в SUI на росте» и «выйти в USDC на падении».

Для каждого правила (часов, порог) хранится монотонная очередь цен за последние N часов: минимум окна (для роста)
или максимум (для падения) берётся за O(1), поэтому правила одинаково быстро работают и на годе 5-минутной
истории, и в бумажном режиме.
"""
from __future__ import annotations

from collections import deque


class RallyWatch:
    def __init__(self, rules: list | None, state=None, min_step: float = 0.0, drop_rules: list | None = None):
        self.rules = [(float(h), float(r)) for h, r in rules or []]
        self.drop_rules = [(float(h), float(r)) for h, r in drop_rules or []]
        if isinstance(state, list):                       # старый формат: только очереди роста
            state = {"rise": state}
        state = state or {}
        self.q = [deque(tuple(x) for x in qs) for qs in state["rise"]] if state.get("rise") else \
            [deque() for _ in self.rules]
        self.dq = [deque(tuple(x) for x in qs) for qs in state["drop"]] if state.get("drop") else \
            [deque() for _ in self.drop_rules]
        self.min_step = min_step          # не чаще раза в min_step секунд (бумажный режим опрашивает часто)
        self.last_t = max((q[-1][0] for q in self.q + self.dq if q), default=0.0)

    def add(self, t: float, p: float):
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
        return {"rise": [[list(x) for x in q] for q in self.q], "drop": [[list(x) for x in q] for q in self.dq]}
