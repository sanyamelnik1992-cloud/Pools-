"""Исследовательский движок торговли (не боевой; боевой бот его не использует).

Движок торговли фьючерсом SUI-USDT-SWAP на часовых свечах OKX. Только прошлые данные, без заглядывания вперёд:
сигнал — по ЗАКРЫТОЙ 4-часовой свече, вход — на открытии следующей часовой свечи. Внутри одной свечи, если
срабатывают и стоп, и цель, считаем, что сработал стоп (осторожная оценка). Фандинг — ставка Binance (каждые 8 ч).
Параметры риска заданы заранее (DEFAULT) и не подбирались."""
import json

DEFAULT = dict(risk=0.01, stop_atr=2.0, tp1_r=2.0, tp1_frac=0.5, trail_atr=3.0,
               max_lev=3.0, fee=0.0005, slip=0.0005, mmr=0.005)

def ema(prev, x, n):
    a = 2 / (n + 1)
    return x if prev is None else a * x + (1 - a) * prev

def load_1h(path="sui_perp_1h.json"):
    """Часовые свечи SUI-USDT-SWAP с OKX (скачиваются отдельно, в git не хранятся)."""
    return json.load(open(path))                                # [t_ms, o, h, l, c, vol]

def load_funding(path="funding_binance.json"):
    """Ставка фандинга SUIUSDT Binance: [время_сек, ставка]."""
    F = json.load(open(path))
    return {int(t * 1000) // 3600000 * 3600000: r for t, r in F}

def daily_phase(rows1h):
    """Фаза рынка по дневным закрытиям (как в боте: средняя 60 дн. ±5%, 2 закрытия). На день d — по закрытиям до d−1."""
    import os, sys
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
    from suibot.rally import Phase
    last = {}
    for r in rows1h:
        last[int(r[0] // 86400000)] = r[4]
    ph, out = Phase(60, 0.05, 0.05, "sma", 2), {}
    for d in sorted(last):
        out[d + 1] = ph.state
        ph._close(d, last[d])
    return out

def bars4h(rows):
    """Закрытые 4-часовые бары: (индекс последней часовой свечи бара, o, h, l, c)."""
    out, cur = [], None
    for i, r in enumerate(rows):
        b = r[0] // 14400000
        if cur is None or cur["b"] != b:
            cur = dict(b=b, o=r[1], h=r[2], l=r[3], c=r[4])
        else:
            cur["h"] = max(cur["h"], r[2]); cur["l"] = min(cur["l"], r[3]); cur["c"] = r[4]
        if i + 1 == len(rows) or rows[i + 1][0] // 14400000 != b:
            out.append((i, cur))
    return out

def side_for(strategy, raw, ph):
    """raw: +1 (пробой вверх по тренду), −1 (вниз), 0 — нет сигнала."""
    if strategy == "trend":                 # и лонг, и шорт, без фильтра фазы
        return raw
    if strategy == "trend_long":            # только лонг
        return 1 if raw > 0 else 0
    if strategy == "trend_phase":           # фаза «рост» — только лонг; «падение» — только шорт
        if ph == "up":
            return 1 if raw > 0 else 0
        if ph == "down":
            return -1 if raw < 0 else 0
        return 0
    raise ValueError(strategy)

def run(strategy, p=DEFAULT, rows=None, funding=None, phase=None, start_equity=200.0, start_ms=None, end_ms=None):
    rows = rows or load_1h()
    funding = funding if funding is not None else load_funding()
    bar_at = dict(bars4h(rows))
    ev, eq = [], []
    E = start_equity
    pos, pending = None, None
    closes, highs, lows, trs = [], [], [], []
    e50 = e200 = None
    liqs = in_pos = 0
    fund_total = 0.0

    def close_pos(px, why, t, frac=1.0):
        nonlocal E, pos
        s, qty = pos["side"], pos["qty"] * frac
        fill = px * (1 - p["slip"]) if s > 0 else px * (1 + p["slip"])
        pnl = s * qty * (fill - pos["entry"])
        fee = qty * fill * p["fee"]
        E += pnl - fee
        pos["realized"] += pnl - fee
        pos["qty"] -= qty
        if pos["qty"] <= 1e-9:
            ev.append(dict(t=t, side=s, entry=pos["entry"], exit=fill, why=why, lev=pos["lev"],
                           R=pos["realized"] / pos["risk_usd"], pnl=pos["realized"], bars=pos["bars"]))
            pos = None

    for i, r in enumerate(rows):
        t, o, h, l, c = r[0], r[1], r[2], r[3], r[4]
        if start_ms and t < start_ms:
            continue
        if end_ms and t > end_ms:
            break
        # 1) сигнал из прошлой закрытой 4-часовой свечи — исполняем на открытии этой свечи
        if pending is not None:
            s, atr = pending
            pending = None
            if pos is not None and pos["side"] != s:
                close_pos(o, "разворот сигнала", t)
            if pos is None and E > 0 and atr > 0:
                px = o * (1 + p["slip"]) if s > 0 else o * (1 - p["slip"])
                dist = p["stop_atr"] * atr
                notional = min(p["risk"] * E / (dist / px), p["max_lev"] * E)   # риск 1% капитала на стоп
                qty = notional / px
                fee = notional * p["fee"]
                lev = notional / E
                pos = dict(side=s, qty=qty, entry=px, stop=px - s * dist, R=dist, realized=-fee,
                           risk_usd=p["risk"] * E, tp1=False, best=px, bars=0, atr=atr, lev=lev, E0=E,
                           liq=px * (1 - 1 / lev + p["mmr"]) if s > 0 else px * (1 + 1 / lev - p["mmr"]))
                E -= fee
        # 2) управление открытой позицией по диапазону свечи
        if pos is not None:
            pos["bars"] += 1
            in_pos += 1
            s = pos["side"]
            if (s > 0 and l <= pos["liq"]) or (s < 0 and h >= pos["liq"]):          # ликвидация: теряем маржу
                liqs += 1
                E -= pos["E0"]
                ev.append(dict(t=t, side=s, entry=pos["entry"], exit=pos["liq"], why="ликвидация", R=-1.0,
                               pnl=-pos["E0"], bars=pos["bars"]))
                pos = None
            else:
                if (s > 0 and l <= pos["stop"]) or (s < 0 and h >= pos["stop"]):
                    close_pos(pos["stop"], "трейлинг-стоп" if pos["tp1"] else "стоп", t)
                if pos is not None and not pos["tp1"]:
                    tp = pos["entry"] + s * p["tp1_r"] * pos["R"]
                    if (s > 0 and h >= tp) or (s < 0 and l <= tp):
                        close_pos(tp, "цель +2R (половина)", t, frac=p["tp1_frac"])
                        if pos is not None:
                            pos["tp1"] = True
                            pos["stop"] = pos["entry"] + s * 1.0 * pos["R"]           # стоп переносим на +1R
                if pos is not None:
                    pos["best"] = max(pos["best"], h) if s > 0 else min(pos["best"], l)
                    if pos["tp1"]:                                                     # трейлинг остатка
                        trail = pos["best"] - s * p["trail_atr"] * pos["atr"]
                        if (s > 0 and trail > pos["stop"]) or (s < 0 and trail < pos["stop"]):
                            pos["stop"] = trail
        # 3) фандинг каждые 8 ч (лонг платит при положительной ставке, шорт получает)
        if pos is not None and t % 28800000 == 0:
            fr = funding.get(t)
            if fr is not None:
                f = pos["qty"] * c * fr * pos["side"]
                E -= f
                fund_total += f
        # 4) закрылась 4-часовая свеча: обновляем индикаторы и сигнал на следующую свечу
        if i in bar_at:
            b = bar_at[i]
            closes.append(b["c"]); highs.append(b["h"]); lows.append(b["l"])
            prev_c = closes[-2] if len(closes) > 1 else b["c"]
            trs.append(max(b["h"] - b["l"], abs(b["h"] - prev_c), abs(b["l"] - prev_c)))
            e50 = ema(e50, b["c"], 50); e200 = ema(e200, b["c"], 200)
            atr = sum(trs[-14:]) / 14 if len(trs) >= 14 else 0.0
            if pos is not None:
                pos["atr"] = atr if atr > 0 else pos["atr"]
            if len(highs) > 21 and atr > 0:
                raw = 0
                if b["c"] > max(highs[-21:-1]) and e50 > e200:
                    raw = 1
                elif b["c"] < min(lows[-21:-1]) and e50 < e200:
                    raw = -1
                s = side_for(strategy, raw, phase.get(t // 86400000) if phase else None)
                if s != 0:
                    pending = (s, atr)
        eq.append((t, E + (pos["qty"] * (c - pos["entry"]) * pos["side"] if pos else 0.0)))
    if pos is not None:
        close_pos(rows[-1][4], "конец данных", rows[-1][0])
    vals = [e for _, e in eq]
    peak, mdd = vals[0], 0.0
    for v in vals:
        peak = max(peak, v); mdd = min(mdd, v / peak - 1)
    wins = [x for x in ev if x["pnl"] > 0]
    gp = sum(x["pnl"] for x in wins); gl = -sum(x["pnl"] for x in ev if x["pnl"] <= 0)
    return dict(ret=E / start_equity - 1, mdd=mdd, trades=len(ev), win=len(wins) / max(len(ev), 1),
                avgR=sum(x["R"] for x in ev) / max(len(ev), 1), pf=(gp / gl) if gl else float("inf"),
                exposure=in_pos / max(len(rows), 1), funding=fund_total, liqs=liqs, events=ev, curve=eq)
