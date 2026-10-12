"""Исследовательский движок «средний срок» (не боевой).

Движок «средний срок»: сигнал на дневном (или 4 ч) таймфрейме, направление — по старшему (дневному/недельному
или фазе). Те же правила риска, что в research/perp_engine.py: риск 1%, стоп 2 ATR, +2R половина, трейлинг 3 ATR.
Все решения — по ЗАКРЫТЫМ барам; старший фильтр — по барам, закрытым не позже текущей свечи (без заглядывания)."""
import bisect, json
import perp_engine as eg

def bars_tf(rows, hours):
    out, cur = [], None
    width = hours * 3600000
    for i, r in enumerate(rows):
        b = r[0] // width
        if cur is None or cur["b"] != b:
            cur = dict(b=b, o=r[1], h=r[2], l=r[3], c=r[4], t0=r[0])
        else:
            cur["h"] = max(cur["h"], r[2]); cur["l"] = min(cur["l"], r[3]); cur["c"] = r[4]
        if i + 1 == len(rows) or rows[i + 1][0] // width != b:
            out.append((i, cur))
    return out

def htf_states(rows, kind, phase):
    """Список (индекс закрытия бара, состояние): 'up'/'down'/None. Для фазы — отдельно."""
    if kind == "phase":
        return None
    hours = 24 if kind == "daily_ema" else 168
    fast, slow = (50, 200) if kind == "daily_ema" else (10, 30)
    out, e1, e2 = [], None, None
    for i, b in bars_tf(rows, hours):
        e1 = eg.ema(e1, b["c"], fast); e2 = eg.ema(e2, b["c"], slow)
        out.append((i, "up" if e1 > e2 else "down"))
    return out

def run_tf(rows, funding, phase, tf_hours=24, donch=20, htf=None, p=None, start_equity=200.0, start_ms=None, end_ms=None):
    p = p or eg.DEFAULT
    sig = dict(bars_tf(rows, tf_hours))              # индекс закрытия -> бар сигнала
    hstates = htf_states(rows, htf, phase) if htf in ("daily_ema", "weekly_ema") else None
    hidx = [x[0] for x in hstates] if hstates else None
    E, pos, pending, ev = start_equity, None, None, []
    closes, highs, lows, trs, in_pos = [], [], [], [], 0
    e_sig = [None, None]
    eq = []
    def close_pos(px, why, t, frac=1.0):
        nonlocal E, pos
        s, qty = pos["side"], pos["qty"] * frac
        fill = px * (1 - p["slip"]) if s > 0 else px * (1 + p["slip"])
        pnl = s * qty * (fill - pos["entry"]); fee = qty * fill * p["fee"]
        E += pnl - fee; pos["realized"] += pnl - fee; pos["qty"] -= qty
        if pos["qty"] <= 1e-9:
            ev.append(dict(t=t, side=s, why=why, pnl=pos["realized"], R=pos["realized"] / pos["risk_usd"], bars=pos["bars"]))
            pos = None
    liqs = 0
    for i, r in enumerate(rows):
        t, o, h, l, c = r[0], r[1], r[2], r[3], r[4]
        if start_ms and t < start_ms: continue
        if end_ms and t > end_ms: break
        if pending is not None:
            s, atr = pending; pending = None
            if pos is not None and pos["side"] != s:
                close_pos(o, "разворот", t)
            if pos is None and E > 0 and atr > 0:
                px = o * (1 + p["slip"]) if s > 0 else o * (1 - p["slip"])
                dist = p["stop_atr"] * atr
                notional = min(p["risk"] * E / (dist / px), p["max_lev"] * E)
                qty = notional / px; fee = notional * p["fee"]; lev = notional / E
                pos = dict(side=s, qty=qty, entry=px, stop=px - s * dist, R=dist, realized=-fee, risk_usd=p["risk"] * E,
                           tp1=False, best=px, bars=0, atr=atr, lev=lev, E0=E,
                           liq=px * (1 - 1 / lev + p["mmr"]) if s > 0 else px * (1 + 1 / lev - p["mmr"]))
                E -= fee
        if pos is not None:
            pos["bars"] += 1; in_pos += 1; s = pos["side"]
            if (s > 0 and l <= pos["liq"]) or (s < 0 and h >= pos["liq"]):
                liqs += 1; E -= pos["E0"]; ev.append(dict(t=t, side=s, why="ликвидация", pnl=-pos["E0"], R=-1.0, bars=pos["bars"])); pos = None
            else:
                if (s > 0 and l <= pos["stop"]) or (s < 0 and h >= pos["stop"]):
                    close_pos(pos["stop"], "трейлинг" if pos["tp1"] else "стоп", t)
                if pos is not None and not pos["tp1"]:
                    tp = pos["entry"] + s * p["tp1_r"] * pos["R"]
                    if (s > 0 and h >= tp) or (s < 0 and l <= tp):
                        close_pos(tp, "+2R (половина)", t, frac=p["tp1_frac"])
                        if pos is not None:
                            pos["tp1"] = True; pos["stop"] = pos["entry"] + s * pos["R"]
                if pos is not None:
                    pos["best"] = max(pos["best"], h) if s > 0 else min(pos["best"], l)
                    if pos["tp1"]:
                        tr = pos["best"] - s * p["trail_atr"] * pos["atr"]
                        if (s > 0 and tr > pos["stop"]) or (s < 0 and tr < pos["stop"]):
                            pos["stop"] = tr
        if pos is not None and t % 28800000 == 0:
            fr = funding.get(t)
            if fr is not None:
                f = pos["qty"] * c * fr * pos["side"]; E -= f
        if i in sig:
            b = sig[i]
            closes.append(b["c"]); highs.append(b["h"]); lows.append(b["l"])
            pc = closes[-2] if len(closes) > 1 else b["c"]
            trs.append(max(b["h"] - b["l"], abs(b["h"] - pc), abs(b["l"] - pc)))
            atr = sum(trs[-14:]) / 14 if len(trs) >= 14 else 0.0
            if pos is not None and atr > 0: pos["atr"] = atr
            if len(highs) > donch + 1 and atr > 0:
                raw = 0
                if b["c"] > max(highs[-donch - 1:-1]): raw = 1
                elif b["c"] < min(lows[-donch - 1:-1]): raw = -1
                # старший фильтр
                allow = None
                if htf == "phase":
                    ph = phase.get(t // 86400000)
                    allow = {"up": 1, "down": -1}.get(ph)
                elif hstates:
                    k = bisect.bisect_right(hidx, i) - 1
                    if k >= 0:
                        allow = 1 if hstates[k][1] == "up" else -1
                if htf == "long_only":
                    allow = 1
                s = raw if (htf in (None,) or (allow is not None and raw == allow)) else 0
                if htf == "long_only" and raw < 0:
                    s = 0
                if s != 0:
                    pending = (s, atr)
        eq.append((t, E + (pos["qty"] * (c - pos["entry"]) * pos["side"] if pos else 0.0)))
    if pos is not None:
        close_pos(rows[-1][4], "конец данных", rows[-1][0])
    vals = [v for _, v in eq]; peak, mdd = vals[0], 0.0
    for v in vals:
        peak = max(peak, v); mdd = min(mdd, v / peak - 1)
    return dict(ret=E / start_equity - 1, mdd=mdd, trades=len(ev), win=sum(1 for x in ev if x["pnl"] > 0) / max(len(ev), 1),
                avgR=sum(x["R"] for x in ev) / max(len(ev), 1), liqs=liqs, exposure=in_pos / max(len(rows), 1), events=ev)
