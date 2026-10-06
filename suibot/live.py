"""Боевой режим: одна стратегия из suibot.toml ([live] strategy) на реальные деньги.

Решения — те же правила, что в бумажном режиме и на истории (book.decide). Транзакции строит и отправляет
исполнитель executor/cli.mjs (официальные SDK Cetus): закрыть позицию с комиссиями и наградами, обменять
через агрегатор до нужной доли, открыть новую позицию. Перед каждой реальной транзакцией — симуляция в сети.

Бот распоряжается только своими деньгами: при старте берёт из кошелька не больше max_capital_usd (оставляя
gas_reserve_sui на газ) и дальше ведёт свой баланс по фактическим изменениям балансов из каждой транзакции.
Правда — в сети: перед действиями после сбоя и раз в полчаса бот сверяется с кошельком (есть ли его позиция,
сколько монет на самом деле), поэтому потерянный ответ сети, закрытый терминал или Ctrl+C посреди пересборки
не приводят ни к двойной трате, ни к зависанию. Позиции, которые были в кошельке до бота, он не трогает.

Ошибка (сеть, проскальзывание) — бот пишет в Telegram, сверяется с кошельком и повторяет через 2, 6 и 18 минут;
после четвёртой ошибки подряд встаёт на паузу до /resume. Прерванные действия (выход в SUI, ручная команда)
доводятся до конца, а не отменяются.

dry_run = true — ничего не отправляется: позиция виртуальная (как в бумажном режиме), а открытие позиции
из реального кошелька один раз проверяется симуляцией. Состояния симуляции и реальных денег хранятся отдельно.

Ручное управление — команды в Telegram или `python3 bot.py control <команда>`:
  status — отчёт;  pause / resume — остановить и продолжить;
  sui — снять позицию и всё в SUI (пауза);  usdc — снять позицию и всё в USDC (пауза);
  close — снять позицию, монеты оставить как есть (пауза). После них /resume открывает позицию заново.
"""
from __future__ import annotations

import fcntl
import json
import math
import os
import subprocess
import time
import traceback
from dataclasses import asdict, replace
from datetime import datetime, timezone

from lpscan.common import ROOT
from sui_pools import SUI, USDC
from suibot import notify
from suibot.book import (Book, accrue_growth, close_to_idle, decide, exit_to_sui, exit_to_usdc, init_book,
                         open_position, rebalance, summary)
from suibot.chain import raw_to_usd, token_price, read_pools, usd_to_raw
from suibot.clmm import amounts, snap_ticks, sqrt_of_tick
from suibot.config import Config
from suibot.paper import _append, _utc, log
from suibot.rally import RallyWatch

GAS_BUFFER_SUI = 0.05      # при открытии позиции столько своих SUI бот оставляет на газ
RETRY_MINUTES = (2, 6, 18)  # повторы после ошибки; следующая ошибка подряд — пауза до /resume
SYNC_MINUTES = 30          # плановая сверка с кошельком
COMMANDS = ("status", "pause", "resume", "sui", "usdc", "close", "help")
HELP = ("Команды: /status — отчёт; /pause — пауза; /resume — продолжить; /sui — снять позицию и всё в SUI; "
        "/usdc — снять позицию и всё в USDC; /close — снять позицию, монеты оставить. После /sui, /usdc, /close "
        "бот на паузе; /resume — снова открыть позицию.")
MANUAL = {"sui": "вручную: всё в SUI", "usdc": "вручную: всё в USDC", "close": "вручную: позиция снята"}


class ExecError(RuntimeError):
    def __init__(self, msg: str, out: dict | None = None):
        super().__init__(msg)
        self.out = out or {}      # ответ исполнителя: sent=false — до сети не дошло; digest — исполнилась с ошибкой


class ReadError(ExecError):
    """Не удалось прочитать кошелёк (сеть) — бот повторяет без паузы: денег это не касается."""


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
        status = out.get("status")
        raise ExecError(out.get("error") or (json.dumps(status, ensure_ascii=False) if status else "")
                        or (r.stderr or "")[-600:] or "исполнитель не ответил", out)
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
        self.dry = self.lc.dry_run
        self.path = self.dir / ("state_dry.json" if self.dry else "state_real.json")   # режимы не смешиваются
        old = self.dir / "state.json"                  # прежняя версия хранила оба режима в одном файле
        if not self.dry and old.exists() and not self.path.exists() and json.loads(old.read_text()).get("pos_id"):
            raise SystemExit(f"найден {old} от прежней версии с реальной позицией: снимите её в Cetus (или прежней "
                             "версией /close), затем удалите этот файл и запустите снова")
        st = json.loads(self.path.read_text()) if self.path.exists() else {}
        self.book = Book(**st["book"]) if st.get("book") else None
        self.pos_id = st.get("pos_id")
        self.paused = st.get("paused", False)
        self.prev = st.get("prev")
        self.tg_offset = st.get("tg_offset")
        self.last_report_day = st.get("last_report_day")
        self.address = st.get("address")
        self.foreign = set(st.get("foreign", []))     # чужие позиции кошелька — бот их не трогает
        self.pending = st.get("pending")               # отправленная, но не подтверждённая транзакция
        self.manual = st.get("manual")                 # ручная команда, которую надо довести до конца
        self.collected = st.get("collected", {})       # собранные награды (CETUS и т.п.), мин. единицы
        self.errors = st.get("errors", 0)
        self.missing = st.get("missing", 0)            # сколько сверок подряд позиция бота не найдена
        self.retry_at = st.get("retry_at")
        self.last_sync = st.get("last_sync", 0.0)
        self.need_sync = not self.dry                  # после каждого запуска — сверка с кошельком
        self.watch = RallyWatch(self.s.rally_exit, st.get("watch"), min_step=60, drop_rules=self.s.crash_exit)
        self.has_key = bool(os.environ.get("SUI_PRIVATE_KEY"))
        self.label = "СИМУЛЯЦИЯ" if self.dry else "РЕАЛЬНЫЕ ДЕНЬГИ"

    # --- служебное ---------------------------------------------------------------------------------------
    def save(self):
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({
            "book": asdict(self.book) if self.book else None, "pos_id": self.pos_id, "paused": self.paused,
            "prev": self.prev, "tg_offset": self.tg_offset, "last_report_day": self.last_report_day,
            "address": self.address, "foreign": sorted(self.foreign), "pending": self.pending, "manual": self.manual,
            "collected": self.collected, "errors": self.errors, "missing": self.missing, "retry_at": self.retry_at,
            "last_sync": self.last_sync, "watch": self.watch.dump()}, ensure_ascii=False))
        tmp.replace(self.path)

    def event(self, t: float, kind: str, price: float, text: str):
        log(f"[{self.label}] {kind}: {text}")
        _append(self.dir / "events.csv", {"time_utc": _utc(t), "mode": self.label, "event": kind,
                                          "price": round(price, 5), "details": text})
        notify.send(f"[{self.label}] {kind}: {text}")

    def price_of(self, st):
        return lambda t: token_price(t, st["sui"])

    def usd(self, a: float, b: float, st) -> float:
        return a * st["ua"] + b * st["ub"]

    def is_sui(self, coin_type: str) -> bool:
        return norm(coin_type) == norm(SUI)

    def report(self, st) -> str:
        if not self.book or not self.book.start:
            return f"[{self.label}] позиция ещё не открыта" + (" (пауза)" if self.paused else "")
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

    # --- реальные транзакции -----------------------------------------------------------------------------
    def ex(self, st, op: dict, *args) -> dict:
        """Реальная транзакция: симуляция, затем подпись и отправка. Пока ответа нет, в состоянии записано, что
        отправлялось и сколько было в кошельке перед отправкой (pending) — если ответ потеряется, сверка с
        кошельком посчитает точную разницу. Снимается pending в apply_changes, когда изменения учтены."""
        sim = executor(*args, simulate=True)
        self.pending = {**op, "before": sim.get("wallet")}
        self.save()
        try:
            return executor(*args, simulate=False)
        except ExecError as e:
            if e.out.get("sent") is False:                 # до сети не дошло — ничего не изменилось
                self.pending = None
            elif e.out.get("digest") and e.out.get("status"):   # исполнилась с ошибкой: списан только газ
                self.apply_changes(e.out, st)
            raise

    def apply_changes(self, res: dict, st, swap: bool = False):
        """Изменения балансов кошелька из транзакции → свои монеты бота вне позиции; награды — отдельно."""
        b = self.book
        da = db = 0.0
        for ch in res.get("balance_changes") or []:
            if ch.get("address") and self.address and ch["address"].lower() != self.address.lower():
                continue
            t, amt = norm(ch["coinType"]), int(ch["amount"])
            if t == norm(self.type_a):
                da += amt
            elif t == norm(self.type_b):
                db += amt
            else:   # награды (CETUS и т.п.) — под исходным типом, по нему DefiLlama находит цену
                self.collected[ch["coinType"]] = self.collected.get(ch["coinType"], 0) + amt
                b.rewards[ch["coinType"]] = b.rewards.get(ch["coinType"], 0.0) + amt
        b.idle_a += da
        b.idle_b += db
        g = res.get("gas") or {}
        gas_sui = (int(g.get("computationCost", 0)) + int(g.get("storageCost", 0))
                   - int(g.get("storageRebate", 0))) / 1e9
        b.costs_usd += -self.usd(da, db, st) if swap else gas_sui * st["sui"]   # обмен: потеря стоимости вместе с газом
        self.pending = None
        self.save()

    def close_real(self, st):
        if not self.pos_id:
            return
        b = self.book
        res = self.ex(st, {"op": "close", "id": self.pos_id}, "close", "--pool", self.pc.object, "--position", self.pos_id,
                      "--band", self.lc.price_band)
        self.forget_position()
        self.apply_changes(res, st)                    # оценка комиссий и наград заменяется фактом

    def forget_position(self):
        b = self.book
        b.L = b.fees_a = b.fees_b = 0.0
        b.rewards = dict(self.collected)
        b.out_since = None
        self.pos_id = None

    def swap_real(self, st, from_a: bool, amount_raw: float):
        b = self.book
        have = b.idle_a if from_a else b.idle_b
        if self.is_sui(self.type_a if from_a else self.type_b):
            have -= GAS_BUFFER_SUI * 1e9                # газ платится из своих SUI бота
        amount = int(min(amount_raw, have))
        if amount <= 0:
            return
        frm, to = (self.type_a, self.type_b) if from_a else (self.type_b, self.type_a)
        res = self.ex(st, {"op": "swap"}, "swap", "--from", frm, "--to", to, "--amount", amount,
                      "--slippage", self.lc.slippage)
        self.apply_changes(res, st, swap=True)

    def to_share(self, st, share_a: float):
        """Обменять свои монеты вне позиции так, чтобы монета a составляла share_a стоимости."""
        va, vb = self.book.idle_a * st["ua"], self.book.idle_b * st["ub"]
        diff = share_a * (va + vb) - va
        if abs(diff) < self.lc.min_swap_usd:
            return
        if diff > 0:
            self.swap_real(st, False, diff / st["ub"])
        else:
            self.swap_real(st, True, -diff / st["ua"])

    def to_coin(self, st, coin: str):
        """Всё своё вне позиции — в SUI или в USDC."""
        if self.dry:
            return
        self.to_share(st, 1.0 if (coin == "sui") == self.pc.a_is_sui else 0.0)

    def wrong_coin_usd(self, st, coin: str) -> float:
        b = self.book
        sui_usd = (b.idle_a if self.pc.a_is_sui else b.idle_b) * (st["ua"] if self.pc.a_is_sui else st["ub"])
        usdc_usd = self.usd(b.idle_a, b.idle_b, st) - sui_usd
        return usdc_usd if coin == "sui" else sui_usd - GAS_BUFFER_SUI * st["sui"]

    def set_position(self, pid: str, liquidity: float, tl: int, th: int):
        b = self.book
        self.pos_id = pid
        b.L, b.tick_lo, b.tick_hi, b.sa = float(liquidity), int(tl), int(th), 0.0
        sa, sb = b.sqrt_bounds()
        b.range_usd = sorted([raw_to_usd(sa * sa, b.a_is_sui), raw_to_usd(sb * sb, b.a_is_sui)])
        b.fees_a = b.fees_b = 0.0
        b.mode, b.out_since = "lp", None

    def open_real(self, st, lo: float, hi: float):
        b = self.book
        tl, th = snap_ticks(usd_to_raw(lo, b.a_is_sui), usd_to_raw(hi, b.a_is_sui), st["spacing"])
        # исполнитель берёт монету a целиком, а монеты b должно хватить при любой цене в коридоре ±price_band —
        # поэтому доля a — наименьшая в коридоре: тогда лишней остаётся только малая часть монеты b
        shares = []
        for k in (1 - self.lc.price_band, 1.0, 1 + self.lc.price_band):
            a1, b1 = amounts(1.0, st["sq"] * math.sqrt(k), sqrt_of_tick(tl), sqrt_of_tick(th))
            shares.append(a1 * st["ua"] / (a1 * st["ua"] + b1 * st["ub"]))
        self.to_share(st, min(shares))
        gas = GAS_BUFFER_SUI * 1e9
        amt_a = int(max(b.idle_a - (gas if b.a_is_sui else 0), 0))
        amt_b = int(max(b.idle_b - (0 if b.a_is_sui else gas), 0))
        if self.usd(amt_a, amt_b, st) < 1:
            raise ExecError("у бота нет монет для позиции")
        res = self.ex(st, {"op": "open", "tl": tl, "th": th}, "open", "--pool", self.pc.object, "--tick-lower", tl,
                      "--tick-upper", th, "--amount-a", amt_a, "--amount-b", amt_b, "--band", self.lc.price_band)
        pos = res.get("position") or {}
        if not pos.get("id"):
            # транзакция прошла, но позиция не опознана: монеты не списываем — сверка найдёт позицию по границам
            self.need_sync = True                     # pending остаётся: сверка найдёт позицию по границам
            self.save()
            raise ExecError("позиция открыта, но не опознана — бот сверится с кошельком")
        self.set_position(pos["id"], pos["liquidity"], tl, th)
        self.apply_changes(res, st)

    def status(self) -> dict:
        args = ["status", "--pool", self.pc.object] + (["--position", self.pos_id] if self.pos_id else [])
        try:
            return executor(*args, simulate=False)
        except (ExecError, subprocess.TimeoutExpired) as e:
            raise ReadError(f"не удалось прочитать кошелёк: {e}") from None

    def available(self, w: dict) -> tuple[float, float]:
        """Монеты кошелька a и b за вычетом резерва на газ."""
        bal = {norm(k): int(v) for k, v in w["balances"].items()}
        sui = max(0, bal.get(norm(SUI), 0) - int(self.lc.gas_reserve_sui * 1e9))
        usdc = bal.get(norm(USDC), 0)
        return (sui, usdc) if self.pc.a_is_sui else (usdc, sui)

    def wallet(self, balances: dict) -> tuple[int, int]:
        bal = {norm(k): int(v) for k, v in (balances or {}).items()}
        return bal.get(norm(self.type_a), 0), bal.get(norm(self.type_b), 0)

    def sync(self, st):
        """Сверка с кошельком: есть ли позиция бота, не появилась ли новая, сколько монет на самом деле."""
        b = self.book
        w = self.status()
        self.address = w["address"]
        budget = self.usd(*b.holdings(st), st)          # стоимость бота до сверки — больше он не возьмёт
        pend = self.pending
        vanished = False
        if self.pos_id and not w.get("position_owned"):
            if not (pend and pend.get("op") == "close"):
                self.missing += 1
                if self.missing < 2:                        # один ответ узла может отставать — перепроверка
                    self.need_sync = True
                    raise ExecError(f"позиция {self.pos_id[:10]}… не найдена в кошельке — перепроверка")
                self.paused = True
                self.event(st["t"], "позиция закрыта не ботом", st["sui"],
                           f"позиции {self.pos_id[:10]}… больше нет в кошельке — бот на паузе; /resume — продолжить")
            self.forget_position()
            vanished = True
        elif self.pos_id and w.get("position"):
            b.L = float(w["position"]["liquidity"])
        self.missing = 0
        adopted = False
        known = self.foreign | ({self.pos_id} if self.pos_id else set())
        for p in w.get("positions", []):
            if p["id"] in known:
                continue
            if (pend and pend.get("op") == "open" and not self.pos_id
                    and (int(p["tick_lower"]), int(p["tick_upper"])) == (pend["tl"], pend["th"])):
                self.set_position(p["id"], p["liquidity"], pend["tl"], pend["th"])
                adopted = True
                self.event(st["t"], "позиция найдена", st["sui"], f"открытие прошло, позиция {p['id'][:10]}…")
            else:
                self.foreign.add(p["id"])
                notify.send(f"[{self.label}] в кошельке новая позиция {p['id'][:10]}… — бот её не трогает")
        avail_a, avail_b = self.available(w)
        if pend and pend.get("before"):
            # точная разница балансов с момента перед отправкой — результат потерянной транзакции
            now_a, now_b = self.wallet(w["balances"])
            was_a, was_b = self.wallet(pend["before"])
            da, db = now_a - was_a, now_b - was_b
            if pend["op"] == "open" and not adopted and not self.pos_id and self.usd(-da, -db, st) > 1:
                self.need_sync = True                       # монеты ушли, а позиции ещё не видно — ждём узел
                raise ExecError("открытие прошло, позиция ещё не видна в кошельке — перепроверка")
            b.idle_a, b.idle_b = b.idle_a + da, b.idle_b + db
        elif pend or vanished:
            # разницы нет (позицию закрыли не через бота): своё — по кошельку, но не больше, чем было у бота
            pos = amounts(b.L, st["sq"], *b.sqrt_bounds()) if b.L else (0.0, 0.0)
            free = budget - self.usd(pos[0] + b.fees_a, pos[1] + b.fees_b, st)
            val = self.usd(avail_a, avail_b, st)
            k = min(1.0, max(0.0, free) / val) if val else 0.0
            b.idle_a, b.idle_b = avail_a * k, avail_b * k
        # бот не может иметь больше, чем есть в кошельке
        b.idle_a, b.idle_b = min(max(b.idle_a, 0.0), avail_a), min(max(b.idle_b, 0.0), avail_b)
        self.pending = None
        self.need_sync, self.last_sync = False, st["t"]
        self.save()

    # --- старт -------------------------------------------------------------------------------------------
    def start_real(self, st):
        w = self.status()
        self.address = w["address"]
        self.foreign = {p["id"] for p in w.get("positions", [])}
        if self.foreign:
            notify.send(f"[{self.label}] в кошельке уже есть позиции в этом пуле ({len(self.foreign)}) — бот их не трогает")
        avail_a, avail_b = self.available(w)
        value = self.usd(avail_a, avail_b, st)
        k = min(1.0, self.lc.max_capital_usd / value) if value else 0.0
        if value * k < 5:
            raise ExecError(f"в кошельке {w['address']} мало средств: ${value:,.2f} без резерва на газ")
        b = self.book = Book(self.s.name, self.s.pool, self.pc.a_is_sui)
        b.idle_a, b.idle_b = avail_a * k, avail_b * k
        capital = value * k
        lo, hi = self.s.target_range(st["sui"], first=True)
        tl, th = snap_ticks(usd_to_raw(lo, b.a_is_sui), usd_to_raw(hi, b.a_is_sui), st["spacing"])
        a1, b1 = amounts(1.0, st["sq"], sqrt_of_tick(tl), sqrt_of_tick(th))
        share_a = a1 * st["ua"] / (a1 * st["ua"] + b1 * st["ub"])       # «та же доля» — как в бумажном режиме
        usdc_share = share_a if not b.a_is_sui else 1 - share_a
        b.start = {"t": st["t"], "price": st["sui"], "capital_sui": capital / st["sui"],
                   "split_sui": capital * (1 - usdc_share) / st["sui"], "split_usdc": capital * usdc_share}
        self.save()
        self.open_real(st, lo, hi)
        lo, hi = b.range_usd
        self.event(st["t"], "открыта", st["sui"], f"кошелёк {w['address'][:10]}…, капитал ${capital:,.2f}, "
                   f"диапазон {lo:.4f}–{hi:.4f}, позиция {self.pos_id[:10]}…")

    def start_dry(self, st):
        s = replace(self.s, capital_sui=self.lc.max_capital_usd / st["sui"])
        self.book = init_book(s, self.pc, st, self.cfg.costs)
        lo, hi = self.book.range_usd
        text = f"виртуальный капитал ${self.lc.max_capital_usd:,.0f}, диапазон {lo:.4f}–{hi:.4f}"
        if self.has_key or self.lc.address:   # проверить, что открытие из реального кошелька проходит симуляцию
            try:
                w = executor("status", "--pool", self.pc.object, simulate=True, address=self.lc.address)
                avail_a, avail_b = self.available(w)
                value = self.usd(avail_a, avail_b, st)
                k = min(1.0, self.lc.max_capital_usd / value) if value else 0.0   # не больше лимита капитала
                a_, b_ = int(avail_a * k), int(avail_b * k)
                if a_ or b_:
                    tl, th = snap_ticks(usd_to_raw(lo, self.pc.a_is_sui), usd_to_raw(hi, self.pc.a_is_sui), st["spacing"])
                    r = executor("open", "--pool", self.pc.object, "--tick-lower", tl, "--tick-upper", th,
                                 "--amount-a", a_, "--amount-b", b_, "--band", self.lc.price_band,
                                 simulate=True, address=self.lc.address)
                    text += f"; симуляция открытия из кошелька {w['address'][:10]}…: успешно ({r['amount_a']} / {r['amount_b']})"
                else:
                    text += f"; в кошельке {w['address'][:10]}… нет SUI/USDC — симуляция открытия пропущена"
            except (ExecError, subprocess.TimeoutExpired) as e:
                text += f"; симуляция не прошла: {e}"
        self.event(st["t"], "открыта", st["sui"], text)

    # --- команды и решения -------------------------------------------------------------------------------
    def read_commands(self) -> list[str]:
        cmds, self.tg_offset = notify.commands(self.tg_offset)
        ctl = self.dir / "control.txt"
        if ctl.exists():
            cmds += [x.strip().lower() for x in ctl.read_text().split() if x.strip()]
            ctl.unlink()
        return cmds

    def command(self, c: str, st):
        if c not in COMMANDS or c == "help":
            notify.send(HELP)
        elif c == "status":
            text = self.report(st)
            if self.errors:
                text += f"\nошибок подряд: {self.errors}"
            notify.send(text)
            log(text)
        elif c == "pause":
            self.paused = True
            self.event(st["t"], "пауза", st["sui"], "по команде; позиция остаётся как есть")
        elif c == "resume":
            self.paused, self.errors, self.retry_at, self.need_sync = False, 0, None, not self.dry
            mode = self.book.mode if self.book else None
            note = {"sui": f"бот в SUI после роста — вернётся в пул после отката на {self.s.resume_drop_pct or 0:.0%}",
                    "usdc": f"бот в USDC после падения — вернётся в пул после отскока на {self.s.resume_rise_pct or 0:.0%}",
                    }.get(mode, "позиция откроется, если её нет")
            if self.manual:
                note = f"сначала будет завершена команда /{self.manual}, после неё бот снова встанет на паузу"
            self.event(st["t"], "продолжение", st["sui"], f"по команде; {note}")
        elif not self.book:
            notify.send(f"[{self.label}] позиция ещё не открыта")
        else:
            self.manual, self.paused = c, True         # выполнится в step и будет доведено до конца при сбое

    def finish(self, st):
        """Довести до конца ручную команду или выход, прерванные ошибкой."""
        b = self.book
        if self.manual:
            what = self.manual
            if self.dry:
                close_to_idle(b, st, self.cfg.costs, what if what in ("sui", "usdc") else None)
            else:
                self.close_real(st)
            b.mode = "hold"
            self.save()
            if what in ("sui", "usdc"):
                self.to_coin(st, what)
            self.manual, self.paused = None, True         # пауза — даже если между сбоем и повтором был /resume
            self.event(st["t"], MANUAL[what], st["sui"], f"пауза до /resume; {self.report(st).splitlines()[1]}")
        elif b.mode in ("sui", "usdc") and not self.dry:
            if self.pos_id:
                self.close_real(st)
            if self.wrong_coin_usd(st, b.mode) >= self.lc.min_swap_usd:
                self.to_coin(st, b.mode)
                self.event(st["t"], "выход завершён", st["sui"], self.report(st).splitlines()[1])

    def act(self, st, kind: str, why: str):
        b, s, p, t = self.book, self.s, st["sui"], st["t"]
        old = list(b.range_usd)
        costs = self.cfg.costs
        if kind == "resume":
            # возврат в пул отмечается до открытия: если открытие сорвётся, бот откроет позицию заново,
            # а не будет менять монеты туда-обратно и не выйдет снова по старому максимуму/минимуму
            b.mode = "lp"
            b.resumes.append(t)
            self.watch.reset()
            self.watch.add(t, p)
            self.save()
        if kind in ("rebalance", "reopen", "resume"):
            if self.dry:
                if kind == "rebalance":
                    rebalance(b, st, s, costs)
                else:
                    open_position(b, st, *s.target_range(p), *b.holdings(st), costs)
            else:
                if kind == "rebalance":
                    self.close_real(st)
                self.open_real(st, *s.target_range(p))
                if kind == "rebalance":
                    b.rebalances.append(t)
        elif kind in ("exit", "crash"):
            to = "sui" if kind == "exit" else "usdc"
            if self.dry:
                (exit_to_sui if to == "sui" else exit_to_usdc)(b, st, costs)
            else:
                self.close_real(st)
                b.mode, b.peak = to, p                  # режим — до обмена: при сбое обмен будет доведён
                (b.exits if to == "sui" else b.crashes).append(t)
                self.save()
                self.to_coin(st, to)
        lo, hi = b.range_usd
        name = {"rebalance": "пересборка", "reopen": "позиция открыта заново", "resume": "возврат в пул",
                "exit": "выход в SUI", "crash": "выход в USDC"}[kind]
        detail = f"{why}: {old[0]:.4f}–{old[1]:.4f} → {lo:.4f}–{hi:.4f}" if kind in ("rebalance", "reopen", "resume") else why
        self.event(t, name, p, f"{detail}; {self.report(st).splitlines()[1]}")

    def step(self, st):
        if self.retry_at and st["t"] < self.retry_at and not self.manual:   # ручную команду не откладываем
            return
        if self.book is None:
            if not self.paused:
                (self.start_dry if self.dry else self.start_real)(st)
                self.watch.add(st["t"], st["sui"])
            self.errors, self.retry_at = 0, None
            return
        if self.paused and self.errors > len(RETRY_MINUTES):   # пауза после ошибок — ждём /resume
            return
        b = self.book
        if not self.dry and (self.need_sync or self.pending or st["t"] - self.last_sync > SYNC_MINUTES * 60):
            self.sync(st)
        self.finish(st)
        if not self.paused:
            if b.mode == "hold":                                     # после /sui, /usdc, /close и /resume
                self.act(st, "resume", "по команде")
            elif b.mode == "lp" and not self.pos_id and not self.dry:   # позиция не открылась или пропала
                d = decide(b, self.s, st, self.watch)
                if d and d[0] in ("exit", "crash"):
                    self.act(st, *d)
                else:
                    self.act(st, "reopen", "позиции нет")
            else:
                d = decide(b, self.s, st, self.watch)
                if d:
                    self.act(st, *d)
        self.errors, self.retry_at = 0, None

    def failed(self, st, e: Exception):
        self.need_sync = not self.dry
        text = str(e) or type(e).__name__
        if isinstance(e, ReadError):                    # нет связи: повторять без паузы, сообщить один раз
            self.errors = min(self.errors + 1, len(RETRY_MINUTES))
            m = RETRY_MINUTES[self.errors - 1]
            self.retry_at = st["t"] + m * 60
            if self.errors == 1:
                self.event(st["t"], "нет связи", st["sui"], f"{text} — повтор через {m} мин")
            else:
                log(f"{text} — повтор через {m} мин")
            return
        self.errors += 1
        if not isinstance(e, (ExecError, subprocess.TimeoutExpired)):
            log(traceback.format_exc())
        if self.errors <= len(RETRY_MINUTES):
            m = RETRY_MINUTES[self.errors - 1]
            self.retry_at = st["t"] + m * 60
            self.event(st["t"], "ошибка", st["sui"], f"{text} — сверка с кошельком и повтор через {m} мин")
        else:
            self.paused, self.retry_at = True, None
            self.event(st["t"], "ошибка", st["sui"], f"{text} — {self.errors}-я ошибка подряд, бот на паузе. "
                       "Деньги в пуле или в кошельке; посмотрите Cetus и пришлите текст ошибки. /resume — продолжить")

    def tick(self, snap: dict):
        st = snap[self.s.pool]
        try:
            if self.book and self.prev and st["t"] > self.prev["t"] and self.book.L:
                accrue_growth(self.book, self.prev, st, self.price_of(st))
            self.watch.add(st["t"], st["sui"])
            for c in self.read_commands():
                self.command(c, st)
            self.step(st)
        except Exception as e:  # noqa: BLE001 — любая ошибка: сверка, повтор, затем пауза; бот не падает
            try:
                self.failed(st, e)
            except Exception:  # noqa: BLE001
                log(traceback.format_exc())
        finally:
            self.prev = {k: st[k] for k in ("t", "sq", "fa", "fb", "rew")}
            day = datetime.now(timezone.utc)
            if day.hour == self.cfg.report_hour_utc and self.last_report_day != day.strftime("%Y-%m-%d"):
                self.last_report_day = day.strftime("%Y-%m-%d")
                try:
                    notify.send(self.report(st))
                except Exception:  # noqa: BLE001
                    log(traceback.format_exc())
            try:
                self.save()
            except OSError as e:
                log(f"не удалось сохранить состояние: {e}")

    def run(self, ticks: int | None = None):
        if not self.dry and not self.has_key:
            raise SystemExit("для реальных денег нужен ключ: read -s SUI_PRIVATE_KEY && export SUI_PRIVATE_KEY "
                             "(или dry_run = true)")
        lock = (self.dir / "lock").open("w")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise SystemExit("бот уже запущен в другом окне — второй экземпляр не нужен") from None
        ctl = self.dir / "control.txt"
        if ctl.exists():                               # команды, отданные, пока бот не работал, не выполняются
            log(f"пропущены старые команды: {' '.join(ctl.read_text().split())}")
            ctl.unlink()
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
            log("остановлен (позиция в пуле остаётся); при следующем запуске бот сверится с кошельком")
        finally:
            self.save()
            lock.close()
        if self.book and self.book.start:
            try:
                print(self.report(read_pools({self.s.pool: self.pc})[self.s.pool]))
            except Exception:  # noqa: BLE001
                pass


def control(cfg: Config, cmd: str):
    """Команда работающему боту через файл (из другого окна терминала)."""
    if cmd not in COMMANDS:
        raise SystemExit(HELP)
    d = cfg.state_dir / "live"
    d.mkdir(parents=True, exist_ok=True)
    with (d / "control.txt").open("a") as f:
        f.write(cmd + "\n")
    print(f"команда «{cmd}» передана боту — выполнится на следующем опросе")
