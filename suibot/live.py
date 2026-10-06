"""Боевой режим: одна стратегия из suibot.toml ([live] strategy) на реальные деньги.

Решения — те же правила, что в бумажном режиме и на истории (book.decide). Транзакции строит и отправляет
исполнитель executor/cli.mjs (официальные SDK Cetus): закрыть позицию с комиссиями и наградами, обменять
через агрегатор до нужной доли, открыть новую позицию. Перед каждой реальной транзакцией — симуляция в сети;
при любой ошибке бот встаёт на паузу и пишет в Telegram.

Бот распоряжается только своими деньгами: при старте берёт из кошелька не больше max_capital_usd (оставляя
gas_reserve_sui на газ) и дальше ведёт свой баланс по фактическим изменениям балансов из каждой транзакции.
dry_run = true — ничего не отправляется: позиция виртуальная (как в бумажном режиме), а открытие позиции
из реального кошелька один раз проверяется симуляцией.

Ручное управление — команды в Telegram или `python3 bot.py control <команда>`:
  status — отчёт;  pause / resume — остановить и продолжить (resume при закрытой позиции открывает новую);
  sui — снять позицию и всё в SUI (пауза);  usdc — снять позицию и всё в USDC (пауза);
  close — снять позицию, монеты оставить как есть (пауза).
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import asdict, replace
from datetime import datetime, timezone

from lpscan.common import ROOT
from sui_pools import SUI, USDC
from suibot import notify
from suibot.book import (Book, accrue_growth, close_to_idle, decide, exit_to_sui, exit_to_usdc, init_book,
                         open_position, rebalance, summary)
from suibot.chain import raw_to_usd, read_pools, token_price, usd_to_raw
from suibot.clmm import amounts, snap_ticks, sqrt_of_tick
from suibot.config import Config
from suibot.paper import _append, _utc, log
from suibot.rally import RallyWatch

GAS_BUFFER_SUI = 0.05      # при открытии позиции столько своих SUI бот оставляет на газ
COMMANDS = ("status", "pause", "resume", "sui", "usdc", "close", "help")
HELP = ("Команды: /status — отчёт; /pause — пауза; /resume — продолжить (открыть позицию, если закрыта); "
        "/sui — снять позицию и всё в SUI; /usdc — снять позицию и всё в USDC; /close — снять позицию, "
        "монеты оставить. После /sui, /usdc, /close бот на паузе до /resume.")


class ExecError(RuntimeError):
    pass


def norm(t: str) -> str:
    """0x000…02::sui::SUI → 0x2::sui::SUI — чтобы сравнивать типы монет."""
    addr, rest = t.split("::", 1)
    return f"{hex(int(addr, 16))}::{rest}"


def executor(*args, simulate: bool, address: str | None = None) -> dict:
    cmd = ["node", str(ROOT / "executor" / "cli.mjs"), *map(str, args)]
    if simulate:
        cmd.append("--simulate")
    if address:
        cmd += ["--address", address]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=240, env={**os.environ, "NODE_NO_WARNINGS": "1"})
    lines = r.stdout.strip().splitlines()
    try:
        out = json.loads(lines[-1]) if lines else {}
    except ValueError:
        raise ExecError((r.stderr or r.stdout)[-600:]) from None
    if not out.get("ok"):
        raise ExecError(out.get("error") or json.dumps(out.get("status"), ensure_ascii=False) or r.stderr[-600:])
    return out


class Live:
    def __init__(self, cfg: Config):
        if not cfg.live:
            raise SystemExit("в suibot.toml нет раздела [live]")
        self.cfg, self.lc = cfg, cfg.live
        self.s = next(s for s in cfg.strategies if s.name == self.lc.strategy)
        self.pc = cfg.pools[self.s.pool]
        self.type_a, self.type_b = (SUI, USDC) if self.pc.a_is_sui else (USDC, SUI)
        self.dir = cfg.state_dir / "live"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = self.dir / "state.json"
        st = json.loads(self.path.read_text()) if self.path.exists() else {}
        self.book = Book(**st["book"]) if st.get("book") else None
        self.pos_id = st.get("pos_id")
        self.paused = st.get("paused", False)
        self.prev = st.get("prev")
        self.tg_offset = st.get("tg_offset")
        self.last_report_day = st.get("last_report_day")
        self.watch = RallyWatch(self.s.rally_exit, st.get("watch"), min_step=60, drop_rules=self.s.crash_exit)
        self.has_key = bool(os.environ.get("SUI_PRIVATE_KEY"))
        self.label = "СИМУЛЯЦИЯ" if self.lc.dry_run else "РЕАЛЬНЫЕ ДЕНЬГИ"

    # --- служебное ---------------------------------------------------------------------------------------
    def save(self):
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"book": asdict(self.book) if self.book else None, "pos_id": self.pos_id,
                                   "paused": self.paused, "prev": self.prev, "tg_offset": self.tg_offset,
                                   "last_report_day": self.last_report_day, "watch": self.watch.dump()},
                                  ensure_ascii=False))
        tmp.replace(self.path)

    def event(self, t: float, kind: str, price: float, text: str):
        log(f"[{self.label}] {kind}: {text}")
        _append(self.dir / "events.csv", {"time_utc": _utc(t), "mode": self.label, "event": kind,
                                          "price": round(price, 5), "details": text})
        notify.send(f"[{self.label}] {kind}: {text}")

    def ex(self, *args) -> dict:
        """Реальная транзакция: сначала симуляция, затем подпись и отправка."""
        executor(*args, simulate=True)
        return executor(*args, simulate=False)

    def apply_changes(self, res: dict):
        """Изменения балансов кошелька из транзакции → свои монеты бота вне позиции; награды — отдельно."""
        for ch in res.get("balance_changes") or []:
            t, amt = norm(ch["coinType"]), int(ch["amount"])
            if t == norm(self.type_a):
                self.book.idle_a += amt
            elif t == norm(self.type_b):
                self.book.idle_b += amt
            else:   # награды (CETUS и т.п.) — под исходным типом, по нему DefiLlama находит цену
                self.book.rewards[ch["coinType"]] = self.book.rewards.get(ch["coinType"], 0.0) + amt

    def price_of(self, st):
        return lambda t: token_price(t, st["sui"])

    def report(self, st) -> str:
        if not self.book:
            return f"[{self.label}] позиция ещё не открыта"
        r = summary(self.book, st, self.price_of(st))
        lo, hi = r["range"]
        state = ("пауза, " if self.paused else "") + ({"sui": "вышел в SUI", "usdc": "вышел в USDC"}.get(r["mode"]) or
                                                       ("без позиции" if not self.book.L else
                                                        "в диапазоне" if r["in_range_now"] else "вне диапазона"))
        return (f"[{self.label}] {self.s.name}: SUI ${r['price']:.4f}, диапазон {lo:.4f}–{hi:.4f}, {state}\n"
                f"стоимость ${r['value']:,.2f} = {r['value_sui']:,.1f} SUI-экв. (старт {r['capital_sui']:,.1f}, "
                f"{r['vs_hold_sui_count']:+,.1f} SUI к холду), к той же доле {r['vs_split']:+,.2f}$\n"
                f"сейчас {r['sui_amount']:,.1f} SUI + {r['usdc_amount']:,.2f} USDC; комиссии ≈${r['fees_usd']:,.2f}, "
                f"издержки ${r['costs_usd']:,.2f}, пересборок {r['rebalances']}, дней {r['days']:.1f}")

    # --- реальные действия -------------------------------------------------------------------------------
    def close_real(self):
        if not self.pos_id:
            return
        res = self.ex("close", "--pool", self.pc.object, "--position", self.pos_id, "--slippage", self.lc.slippage)
        self.book.L = self.book.fees_a = self.book.fees_b = 0.0     # оценка комиссий заменяется фактом
        self.apply_changes(res)
        self.pos_id = None

    def swap_real(self, from_a: bool, amount_raw: float):
        frm, to = (self.type_a, self.type_b) if from_a else (self.type_b, self.type_a)
        res = self.ex("swap", "--from", frm, "--to", to, "--amount", int(amount_raw), "--slippage", self.lc.slippage)
        self.apply_changes(res)

    def to_share(self, st, share_a: float):
        """Обменять свои монеты вне позиции так, чтобы монета a составляла share_a стоимости."""
        va, vb = self.book.idle_a * st["ua"], self.book.idle_b * st["ub"]
        diff = share_a * (va + vb) - va
        if abs(diff) < self.lc.min_swap_usd:
            return
        if diff > 0:
            self.swap_real(False, diff / st["ub"])
        else:
            self.swap_real(True, -diff / st["ua"])

    def open_real(self, st, lo: float, hi: float):
        b = self.book
        tl, th = snap_ticks(usd_to_raw(lo, b.a_is_sui), usd_to_raw(hi, b.a_is_sui), st["spacing"])
        sa, sb = sqrt_of_tick(tl), sqrt_of_tick(th)
        a1, b1 = amounts(1.0, st["sq"], sa, sb)
        self.to_share(st, a1 * st["ua"] / (a1 * st["ua"] + b1 * st["ub"]))
        gas = GAS_BUFFER_SUI * 1e9                                # газ платится из своих SUI бота, а не из резерва
        amt_a = max(b.idle_a - (gas if b.a_is_sui else 0), 0)
        amt_b = max(b.idle_b - (0 if b.a_is_sui else gas), 0)
        res = self.ex("open", "--pool", self.pc.object, "--tick-lower", tl, "--tick-upper", th,
                      "--amount-a", int(amt_a), "--amount-b", int(amt_b), "--slippage", self.lc.slippage)
        self.apply_changes(res)
        pos = res.get("position") or {}
        if not pos.get("id"):
            raise ExecError("позиция открыта, но не найдена в кошельке — проверьте Cetus и состояние бота")
        self.pos_id = pos["id"]
        b.L, b.tick_lo, b.tick_hi, b.sa = float(pos["liquidity"]), tl, th, 0.0
        b.range_usd = sorted([raw_to_usd(sa * sa, b.a_is_sui), raw_to_usd(sb * sb, b.a_is_sui)])
        b.fees_a = b.fees_b = 0.0
        b.mode, b.out_since = "lp", None

    def start_real(self, st):
        w = executor("status", "--pool", self.pc.object, simulate=False)
        if w.get("positions"):
            notify.send(f"[{self.label}] в кошельке уже есть позиции в этом пуле — бот их не трогает")
        bal = {norm(k): int(v) for k, v in w["balances"].items()}
        sui = max(0, bal.get(norm(SUI), 0) - int(self.lc.gas_reserve_sui * 1e9))
        usdc = bal.get(norm(USDC), 0)
        value = sui / 1e9 * st["sui"] + usdc / 1e6
        k = min(1.0, self.lc.max_capital_usd / value) if value else 0.0
        if value * k < 5:
            raise ExecError(f"в кошельке {w['address']} мало средств: ${value:,.2f} без резерва на газ")
        b = self.book = Book(self.s.name, self.s.pool, self.pc.a_is_sui)
        b.idle_a, b.idle_b = (sui * k, usdc * k) if self.pc.a_is_sui else (usdc * k, sui * k)
        capital = value * k
        self.open_real(st, *self.s.target_range(st["sui"], first=True))
        pa, pb = amounts(b.L, st["sq"], *b.sqrt_bounds())
        sui_pos, usdc_pos = (pa / 1e9, pb / 1e6) if b.a_is_sui else (pb / 1e9, pa / 1e6)
        b.start = {"t": st["t"], "price": st["sui"], "capital_sui": capital / st["sui"],
                   "split_sui": sui_pos, "split_usdc": usdc_pos}
        lo, hi = b.range_usd
        self.event(st["t"], "открыта", st["sui"], f"кошелёк {w['address'][:10]}…, капитал ${capital:,.2f}, "
                   f"диапазон {lo:.4f}–{hi:.4f}, позиция {self.pos_id[:10]}…")

    # --- виртуальный режим (dry_run) ---------------------------------------------------------------------
    def start_dry(self, st):
        s = replace(self.s, capital_sui=self.lc.max_capital_usd / st["sui"])
        self.book = init_book(s, self.pc, st, self.cfg.costs)
        lo, hi = self.book.range_usd
        text = f"виртуальный капитал ${self.lc.max_capital_usd:,.0f}, диапазон {lo:.4f}–{hi:.4f}"
        if self.has_key or self.lc.address:   # проверить, что открытие из реального кошелька проходит симуляцию
            try:
                w = executor("status", "--pool", self.pc.object, simulate=True, address=self.lc.address)
                bal = {norm(k): int(v) for k, v in w["balances"].items()}
                sui = max(0, bal.get(norm(SUI), 0) - int(self.lc.gas_reserve_sui * 1e9))
                usdc = bal.get(norm(USDC), 0)
                value = sui / 1e9 * st["sui"] + usdc / 1e6
                k = min(1.0, self.lc.max_capital_usd / value) if value else 0.0   # не больше лимита капитала
                sui, usdc = int(sui * k), int(usdc * k)
                if sui or usdc:
                    tl, th = snap_ticks(usd_to_raw(lo, self.pc.a_is_sui), usd_to_raw(hi, self.pc.a_is_sui), st["spacing"])
                    a_, b_ = (sui, usdc) if self.pc.a_is_sui else (usdc, sui)
                    r = executor("open", "--pool", self.pc.object, "--tick-lower", tl, "--tick-upper", th,
                                 "--amount-a", a_, "--amount-b", b_, "--slippage", self.lc.slippage,
                                 simulate=True, address=self.lc.address)
                    text += f"; симуляция открытия из кошелька {w['address'][:10]}…: успешно ({r['amount_a']} / {r['amount_b']})"
                else:
                    text += f"; в кошельке {w['address'][:10]}… нет SUI/USDC — симуляция открытия пропущена"
            except (ExecError, subprocess.TimeoutExpired) as e:
                text += f"; симуляция не прошла: {e}"
        self.event(st["t"], "открыта", st["sui"], text)

    # --- команды и решения -------------------------------------------------------------------------------
    def command(self, c: str, st):
        if c not in COMMANDS:
            notify.send(HELP)
            return
        if c == "help":
            notify.send(HELP)
        elif c == "status":
            notify.send(self.report(st))
            log(self.report(st))
        elif c == "pause":
            self.paused = True
            self.event(st["t"], "пауза", st["sui"], "по команде; позиция остаётся как есть")
        elif c == "resume":
            self.paused = False
            if self.book and not self.book.L:
                self.act(st, "resume", "по команде")
            else:
                self.event(st["t"], "продолжение", st["sui"], "по команде")
        else:
            to = {"sui": "sui", "usdc": "usdc", "close": None}[c]
            self.act(st, "manual", to)
            self.paused = True

    def act(self, st, kind: str, why):
        b, s, p, t = self.book, self.s, st["sui"], st["t"]
        old = b.range_usd
        if self.lc.dry_run:                                           # виртуальное исполнение
            if kind == "rebalance":
                rebalance(b, st, s, self.cfg.costs)
            elif kind == "exit":
                exit_to_sui(b, st, self.cfg.costs)
            elif kind == "crash":
                exit_to_usdc(b, st, self.cfg.costs)
            elif kind == "resume":
                a, bb = b.holdings(st)
                open_position(b, st, *s.target_range(p), a, bb, self.cfg.costs)
                b.resumes.append(t)
                self.watch.reset()
            else:
                close_to_idle(b, st, self.cfg.costs, why)
                b.mode = "hold"
        else:
            if kind == "rebalance":
                self.close_real()
                self.open_real(st, *s.target_range(p))
                b.rebalances.append(t)
            elif kind == "exit":
                self.close_real()
                self.to_share(st, 1.0 if b.a_is_sui else 0.0)
                b.mode, b.peak = "sui", p
                b.exits.append(t)
            elif kind == "crash":
                self.close_real()
                self.to_share(st, 0.0 if b.a_is_sui else 1.0)
                b.mode, b.peak = "usdc", p
                b.crashes.append(t)
            elif kind == "resume":
                self.open_real(st, *s.target_range(p))
                b.resumes.append(t)
                self.watch.reset()
            else:
                self.close_real()
                if why == "sui":
                    self.to_share(st, 1.0 if b.a_is_sui else 0.0)
                elif why == "usdc":
                    self.to_share(st, 0.0 if b.a_is_sui else 1.0)
                b.mode = "hold"
        lo, hi = b.range_usd
        name = ({"rebalance": "пересборка", "exit": "выход в SUI", "crash": "выход в USDC",
                 "resume": "возврат в пул"}.get(kind)
                or {"sui": "вручную: всё в SUI", "usdc": "вручную: всё в USDC"}.get(why, "вручную: позиция снята"))
        detail = (f"{why}: {old[0]:.4f}–{old[1]:.4f} → {lo:.4f}–{hi:.4f}" if kind in ("rebalance", "resume")
                  else "пауза до /resume" if kind == "manual" else str(why))
        self.event(t, name, p, f"{detail}; {self.report(st).splitlines()[1]}")

    def tick(self, snap: dict):
        st = snap[self.s.pool]
        cmds, self.tg_offset = notify.commands(self.tg_offset)
        ctl = self.dir / "control.txt"
        if ctl.exists():
            cmds += [x.strip().lower() for x in ctl.read_text().split() if x.strip()]
            ctl.unlink()
        try:
            if self.book is None:
                if not self.paused:
                    (self.start_dry if self.lc.dry_run else self.start_real)(st)
                    self.watch.add(st["t"], st["sui"])
            else:
                if self.prev and st["t"] > self.prev["t"] and self.book.L:
                    accrue_growth(self.book, self.prev, st, self.price_of(st))
                for c in cmds:
                    self.command(c, st)
                if not self.paused:
                    d = decide(self.book, self.s, st, self.watch)
                    if d:
                        self.act(st, *d)
        except (ExecError, subprocess.TimeoutExpired) as e:
            self.paused = True
            self.event(st["t"], "ошибка", st["sui"], f"{e} — бот на паузе, проверьте кошелёк и Cetus; /resume — продолжить")
        self.prev = {k: st[k] for k in ("t", "sq", "fa", "fb", "rew")}
        day = datetime.now(timezone.utc)
        if day.hour == self.cfg.report_hour_utc and self.last_report_day != day.strftime("%Y-%m-%d"):
            self.last_report_day = day.strftime("%Y-%m-%d")
            notify.send(self.report(st))
        self.save()

    def run(self, ticks: int | None = None):
        if not self.lc.dry_run and not self.has_key:
            raise SystemExit("для реальных денег нужен ключ: export SUI_PRIVATE_KEY=suiprivkey1…  (или dry_run = true)")
        log(f"боевой режим [{self.label}]: «{self.s.name}», до ${self.lc.max_capital_usd:,.0f}, опрос каждые "
            f"{self.cfg.poll_seconds} с; команды — Telegram или python3 bot.py control <команда>")
        n = 0
        try:
            while ticks is None or n < ticks:
                try:
                    snap = read_pools({self.s.pool: self.pc})
                except Exception as e:  # noqa: BLE001 — сеть и лимиты не должны останавливать бота
                    log(f"не удалось прочитать пул: {e}")
                else:
                    self.tick(snap)
                n += 1
                if ticks is None or n < ticks:
                    time.sleep(self.cfg.poll_seconds)
        except KeyboardInterrupt:
            log("остановлен, состояние сохранено (позиция в пуле остаётся)")
        if self.book:
            print(self.report(read_pools({self.s.pool: self.pc})[self.s.pool]))


def control(cfg: Config, cmd: str):
    """Команда работающему боту через файл (из другого окна терминала)."""
    if cmd not in COMMANDS:
        raise SystemExit(HELP)
    d = cfg.state_dir / "live"
    d.mkdir(parents=True, exist_ok=True)
    with (d / "control.txt").open("a") as f:
        f.write(cmd + "\n")
    print(f"команда «{cmd}» передана боту — выполнится на следующем опросе")
