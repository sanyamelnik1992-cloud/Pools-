"""Бумажный фьючерс «пробой 20 дн.» (тень, без денег, без ключа). Работает по ценам пула, которые бумажный режим
читает каждые poll_seconds. Правила — как в research/trend_tf.py (дневной сигнал, без старших фильтров):

- дневной бар (UTC) — по ценам пула; сигнал — пробой 20-дневного максимума (лонг) или минимума (шорт) по закрытому дню;
- вход — первой ценой следующего дня (как открытие следующей свечи в исследовании);
- стоп 2 ATR(14) дневных; при +2R закрывается половина, стоп остатка — на +1R; дальше трейлинг 3 ATR от лучшей цены;
- противоположный сигнал закрывает позицию и открывает новую;
- весь капитал: номинал не больше max_lev × капитала (по умолчанию 1× — без заёмных денег).

Фандинг не учтён (в исследовании он около +1.7 $ за весь период — почти ноль). Издержки: комиссия и проскальзывание.
Состояние сохраняется в состоянии бумажного режима (ключ «perp»). Ничего не отправляет и не подписывает.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass
class PerpCfg:
    pool: str = "cetus_005"        # пул, по ценам которого считается схема (должен быть в стратегиях бумажного режима)
    capital_usd: float = 200.0     # стартовый капитал тени
    risk: float = 1.0              # доля капитала, которую отдаём на стопе; 1.0 с max_lev 1.0 — весь капитал
    max_lev: float = 1.0           # номинал не больше стольких капиталов (1.0 — без плеча)
    stop_atr: float = 2.0          # стоп: 2 ATR
    tp1_r: float = 2.0             # первая цель: +2R
    tp1_frac: float = 0.5          # при первой цели закрывается эта доля позиции
    trail_atr: float = 3.0         # трейлинг остатка: 3 ATR от лучшей цены
    donch: int = 20                # пробой 20 дневных баров
    fee: float = 0.0005            # комиссия за сторону
    slip: float = 0.0005           # проскальзывание за сторону
    mmr: float = 0.005             # поддерживающая маржа (нужна только при плече больше 1)
    enabled: bool = True


class PerpShadow:
    def __init__(self, cfg: PerpCfg, state: dict | None = None):
        self.cfg = cfg
        st = state or {}
        self.E = st.get("E", cfg.capital_usd)            # реализованный капитал, $
        self.start_price = st.get("start_price")
        self.day = st.get("day")                          # индекс дня UTC текущего бара
        self.bar = st.get("bar")                          # {"o","h","l","c"} текущего дня
        self.highs = st.get("highs", [])                  # закрытые дневные бары (хвост)
        self.lows = st.get("lows", [])
        self.closes = st.get("closes", [])
        self.trs = st.get("trs", [])
        self.pending = st.get("pending")                  # (сторона, ATR) — войти на первой цене следующего дня
        self.pos = st.get("pos")                          # открытая позиция или None
        self.peak = st.get("peak", self.E)
        self.mdd = st.get("mdd", 0.0)
        self.trades = st.get("trades", [])                # закрытые сделки (хвост)
        self.last_price = st.get("last_price")
        self.last_t = st.get("last_t")
        self.last_day = st.get("last_day")                # последний закрытый день в истории

    # --- состояние -------------------------------------------------------------------------------------
    def to_dict(self) -> dict:
        return dict(E=self.E, start_price=self.start_price, day=self.day, bar=self.bar, highs=self.highs[-60:],
                    lows=self.lows[-60:], closes=self.closes[-60:], trs=self.trs[-30:], pending=self.pending,
                    pos=self.pos, peak=self.peak, mdd=self.mdd, trades=self.trades[-200:],
                    last_price=self.last_price, last_t=self.last_t, last_day=self.last_day)

    # --- расчёты ---------------------------------------------------------------------------------------
    def _atr(self) -> float:
        return sum(self.trs[-14:]) / 14 if len(self.trs) >= 14 else 0.0

    def _equity(self, p: float) -> float:
        if not self.pos:
            return self.E
        return self.E + self.pos["side"] * self.pos["qty"] * (p - self.pos["entry"])

    def _close(self, px: float, why: str, t: float, frac: float = 1.0) -> str:
        pos, c = self.pos, self.cfg
        s, qty = pos["side"], pos["qty"] * frac
        fill = px * (1 - c.slip) if s > 0 else px * (1 + c.slip)
        pnl = s * qty * (fill - pos["entry"])
        fee = qty * fill * c.fee
        self.E += pnl - fee
        pos["realized"] += pnl - fee
        pos["qty"] -= qty
        text = f"{why}: цена ${fill:.4f}, вход ${pos['entry']:.4f}"
        if pos["qty"] <= 1e-9:
            R = pos["realized"] / pos["risk_usd"] if pos["risk_usd"] else 0.0
            self.trades.append(dict(t=t, side=s, entry=pos["entry"], exit=fill, pnl=round(pos["realized"], 2),
                                    R=round(R, 2), why=why))
            text += f"; итог сделки {pos['realized']:+.2f} $ ({R:+.1f}R)"
            self.pos = None
        return text

    def _enter(self, s: int, atr: float, p: float, t: float) -> str:
        c = self.cfg
        px = p * (1 + c.slip) if s > 0 else p * (1 - c.slip)
        dist = c.stop_atr * atr
        notional = min(c.risk * self.E / (dist / px), c.max_lev * self.E)
        qty = notional / px
        fee = notional * c.fee
        lev = notional / self.E
        risk_usd = c.risk * self.E
        self.E -= fee
        self.pos = dict(side=s, qty=qty, entry=px, stop=px - s * dist, R=dist, realized=-fee, risk_usd=risk_usd,
                        tp1=False, best=px, atr=atr, lev=lev, E0=self.E,
                        liq=px * (1 - 1 / lev + c.mmr) if s > 0 else px * (1 + 1 / lev - c.mmr))
        side = "лонг" if s > 0 else "шорт"
        return (f"открыт {side} по пробою {c.donch} дн.: цена ${px:.4f}, стоп ${self.pos['stop']:.4f} "
                f"(2 ATR), номинал ${notional:.2f} из капитала ${self.E:.2f}")

    def need_warm(self) -> bool:
        """Нужна дневная история: без неё сигнал появится только через ~3 недели."""
        return len(self.highs) < self.cfg.donch + 2 or len(self.trs) < 15

    def warm(self, bars: list) -> int:
        """Прогрев закрытыми днями: [(индекс дня, открытие, максимум, минимум, закрытие)]. Дни, уже учтённые
        или сегодняшние, пропускаются. Возвращает, сколько дней добавлено."""
        added = 0
        for d, o, hi, lo, c in sorted(bars):
            if (self.last_day is not None and d <= self.last_day) or (self.day is not None and d >= self.day):
                continue
            pc = self.closes[-1] if self.closes else o
            self.highs.append(hi); self.lows.append(lo); self.closes.append(c)
            self.trs.append(max(hi - lo, abs(hi - pc), abs(lo - pc)))
            self.last_day = d
            added += 1
        self.highs, self.lows = self.highs[-60:], self.lows[-60:]
        self.closes, self.trs = self.closes[-60:], self.trs[-30:]
        return added

    def _close_day(self, events: list, t: float):
        """Закрылся дневной бар: обновить ATR и пробой, назначить вход на следующий день."""
        b = self.bar
        pc = self.closes[-1] if self.closes else b["o"]
        self.highs.append(b["h"]); self.lows.append(b["l"]); self.closes.append(b["c"])
        self.last_day = self.day
        self.trs.append(max(b["h"] - b["l"], abs(b["h"] - pc), abs(b["l"] - pc)))
        atr = self._atr()
        if self.pos and atr > 0:                 # трейлинг считается от текущего ATR (как в исследовании)
            self.pos["atr"] = atr
        self.pending = None
        if len(self.highs) > self.cfg.donch + 1 and atr > 0:
            raw = 0
            if b["c"] > max(self.highs[-self.cfg.donch - 1:-1]):
                raw = 1
            elif b["c"] < min(self.lows[-self.cfg.donch - 1:-1]):
                raw = -1
            if raw:
                self.pending = (raw, atr)

    # --- главный вход: каждая цена пула ----------------------------------------------------------------
    def on_price(self, t: float, p: float) -> list[tuple[str, str]]:
        """Вернуть события (вид, текст) для журнала и Telegram. Вызывать на каждой цене пула."""
        ev: list[tuple[str, str]] = []
        if self.start_price is None:
            self.start_price = p
        day = int(t // 86400)
        new_day = self.day is not None and day != self.day
        if self.day is None:
            self.day, self.bar = day, dict(o=p, h=p, l=p, c=p)
        elif new_day:
            self._close_day(ev, t)
            self.day, self.bar = day, dict(o=p, h=p, l=p, c=p)
        else:
            self.bar["h"] = max(self.bar["h"], p); self.bar["l"] = min(self.bar["l"], p); self.bar["c"] = p
        # 1) сигнал прошлого дня — вход первой ценой нового дня
        if new_day and self.pending:
            s, atr = self.pending
            self.pending = None
            if self.pos and self.pos["side"] != s:
                ev.append(("разворот", self._close(p, "разворот сигнала", t)))
            if self.pos is None and self.E > 0 and atr > 0:
                ev.append(("вход", self._enter(s, atr, p, t)))
        # 2) управление позицией по текущей цене
        if self.pos:
            c, pos = self.cfg, self.pos
            s = pos["side"]
            if (s > 0 and p <= pos["liq"]) or (s < 0 and p >= pos["liq"]):
                pos["qty"] = 0.0
                self.E -= pos["E0"]
                self.pos = None
                ev.append(("ликвидация", f"ликвидация по цене ${p:.4f}: маржа потеряна"))
            else:
                if (s > 0 and p <= pos["stop"]) or (s < 0 and p >= pos["stop"]):
                    ev.append(("стоп" if not pos["tp1"] else "трейлинг", self._close(
                        pos["stop"], "стоп" if not pos["tp1"] else "трейлинг-стоп", t)))
                if self.pos and not self.pos["tp1"]:
                    tp = self.pos["entry"] + s * c.tp1_r * self.pos["R"]
                    if (s > 0 and p >= tp) or (s < 0 and p <= tp):
                        ev.append(("цель", self._close(tp, f"+{c.tp1_r:g}R: половина закрыта", t, frac=c.tp1_frac)))
                        if self.pos:
                            self.pos["tp1"] = True
                            self.pos["stop"] = self.pos["entry"] + s * self.pos["R"]    # стоп на +1R
                if self.pos:
                    self.pos["best"] = max(self.pos["best"], p) if s > 0 else min(self.pos["best"], p)
                    if self.pos["tp1"]:
                        tr = self.pos["best"] - s * c.trail_atr * self.pos["atr"]
                        if (s > 0 and tr > self.pos["stop"]) or (s < 0 and tr < self.pos["stop"]):
                            self.pos["stop"] = tr
        # 3) учёт просадки по эквити
        eq = self._equity(p)
        self.peak = max(self.peak, eq)
        self.mdd = min(self.mdd, eq / self.peak - 1)
        self.last_price, self.last_t = p, t
        return ev

    # --- сводка для отчёта -----------------------------------------------------------------------------
    def summary(self) -> dict:
        p = self.last_price or self.start_price or 0.0
        eq = self._equity(p)
        hold = (p / self.start_price - 1) if self.start_price else 0.0
        wins = sum(1 for x in self.trades if x["pnl"] > 0)
        return dict(equity=eq, ret=eq / self.cfg.capital_usd - 1, hold=hold, mdd=self.mdd, trades=len(self.trades),
                    win=(wins / len(self.trades)) if self.trades else 0.0, pos=self.pos and dict(
                        side=self.pos["side"], entry=self.pos["entry"], stop=self.pos["stop"], tp1=self.pos["tp1"]))

    def line(self) -> str:
        s = self.summary()
        pos = s["pos"]
        where = ("нет позиции" if not pos else f"{'лонг' if pos['side'] > 0 else 'шорт'} от ${pos['entry']:.4f}, "
                 f"стоп ${pos['stop']:.4f}" + (", половина закрыта" if pos["tp1"] else ""))
        return (f"🧪 Тень «пробой {self.cfg.donch} дн., весь капитал»: ${s['equity']:.2f} ({s['ret']:+.0%}), "
                f"держать SUI {s['hold']:+.0%}, просадка {s['mdd']:+.0%}, сделок {s['trades']}, {where}")
