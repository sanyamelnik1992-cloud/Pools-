"""Бумажный режим: виртуальные позиции на живых данных пулов, без ключей и денег.

Каждые poll_seconds бот читает пул, начисляет каждой стратегии комиссии и награды по приросту счётчиков
пула, пока цена в её диапазоне, и пересобирает позицию по правилам стратегии (с издержками обмена и газа).
Состояние сохраняется после каждого такта, поэтому бота можно останавливать и запускать снова; время,
когда он не работал, учитывается как пропуск (начисление за него приблизительное).
"""
from __future__ import annotations

import csv
import json
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from suibot import notify, report
from suibot.book import Book, accrue_growth, check_stop, init_book, step, summary
from suibot.chain import read_pools, token_price
from suibot.config import Config
from suibot.rally import RallyWatch


def log(*a):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), *a, flush=True)


def _append(path: Path, row: dict):
    new = not path.exists()
    with path.open("a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(row))
        if new:
            w.writeheader()
        w.writerow(row)


def _utc(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


class Paper:
    def __init__(self, cfg: Config, reset: bool = False):
        self.cfg = cfg
        cfg.state_dir.mkdir(parents=True, exist_ok=True)
        self.path = cfg.state_dir / "state.json"
        if reset and self.path.exists():
            self.path.unlink()
        st = json.loads(self.path.read_text()) if self.path.exists() else {}
        self.books = {n: Book(**d) for n, d in st.get("books", {}).items()}
        self.prev = st.get("prev", {})
        self.last_snapshot = st.get("last_snapshot", 0)
        self.last_report_day = st.get("last_report_day")
        self.watch_state = st.get("watch", {})
        self.watches: dict[str, RallyWatch] = {}

    def watch(self, s) -> RallyWatch:
        """Наблюдатель роста стратегии (цены не чаще раза в минуту, переживает перезапуск)."""
        if s.name not in self.watches:
            self.watches[s.name] = RallyWatch(s.rally_exit, self.watch_state.get(s.name), min_step=60,
                                              drop_rules=s.crash_exit)
        return self.watches[s.name]

    def save(self):
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"books": {n: asdict(b) for n, b in self.books.items()}, "prev": self.prev,
                                   "last_snapshot": self.last_snapshot, "last_report_day": self.last_report_day,
                                   "watch": {**self.watch_state, **{n: w.dump() for n, w in self.watches.items()}}},
                                  ensure_ascii=False))
        tmp.replace(self.path)

    def event(self, t: float, name: str, kind: str, price: float, text: str, tg: bool = True):
        log(f"[{name}] {kind}: {text}")
        _append(self.cfg.state_dir / "events.csv",
                {"time_utc": _utc(t), "strategy": name, "event": kind, "price": round(price, 5), "details": text})
        if tg:
            notify.send(f"[{name}] {kind}: {text}")

    def rows(self, snap: dict) -> list[dict]:
        return [summary(self.books[s.name], snap[s.pool], lambda t, p=snap[s.pool]["sui"]: token_price(t, p))
                for s in self.cfg.strategies if s.name in self.books]

    def tick(self, snap: dict):
        cfg = self.cfg
        gaps = {k: st["t"] - self.prev[k]["t"] for k, st in snap.items()
                if k in self.prev and st["t"] - self.prev[k]["t"] > 5 * cfg.poll_seconds}
        for k, dt in gaps.items():
            log(f"пул {k}: перерыв {dt / 60:.0f} мин — начисление за это время приблизительное")
        for s in cfg.strategies:
            st = snap[s.pool]

            def price_of(t, p=st["sui"]):
                return token_price(t, p)

            book = self.books.get(s.name)
            if book is None:
                book = self.books[s.name] = init_book(s, cfg.pools[s.pool], st, cfg.costs)
                self.watch(s).add(st["t"], st["sui"])
                lo, hi = book.range_usd
                self.event(st["t"], s.name, "открыта", st["sui"],
                           f"{s.capital_sui:,.0f} SUI, диапазон {lo:.4f}–{hi:.4f}, SUI ${st['sui']:.4f}, "
                           f"доля SUI {book.start['split_sui'] * st['sui'] / (book.start['split_sui'] * st['sui'] + book.start['split_usdc']):.0%}")
                continue
            prev = self.prev.get(s.pool)
            if prev and st["t"] > prev["t"]:
                if s.pool in gaps:
                    book.gap_s += gaps[s.pool]
                accrue_growth(book, prev, st, price_of)
            ev = step(book, s, st, self.watch(s), cfg.costs)
            if ev:
                self.event(st["t"], s.name, ev[0], st["sui"], ev[1])
            summ = summary(book, st, price_of)
            if check_stop(book, s, summ):
                self.event(st["t"], s.name, "остановка", st["sui"],
                           f"пул отстал от «той же доли» на ${-summ['vs_split']:,.0f} — пересборки прекращены")
        self.prev = {k: {key: st[key] for key in ("t", "sq", "fa", "fb", "rew")} for k, st in snap.items()}
        now = time.time()
        if now - self.last_snapshot >= cfg.snapshot_minutes * 60:
            self.last_snapshot = now
            for r in self.rows(snap):
                _append(cfg.state_dir / "snapshots.csv",
                        {"time_utc": _utc(now), "strategy": r["name"], "price": round(r["price"], 5),
                         "in_range": int(r["in_range_now"]), "value": round(r["value"], 2),
                         "hold_sui": round(r["hold_sui"], 2), "hold_split": round(r["hold_split"], 2),
                         "fees_usd": round(r["fees_usd"], 2), "costs_usd": round(r["costs_usd"], 2),
                         "rebalances": r["rebalances"], "mode": r["mode"]})
        day = datetime.now(timezone.utc)
        if day.hour == cfg.report_hour_utc and self.last_report_day != day.strftime("%Y-%m-%d"):
            self.last_report_day = day.strftime("%Y-%m-%d")
            notify.send("Отчёт бумажного бота\n" + report.short(self.rows(snap)))
        self.save()

    def run(self, ticks: int | None = None):
        cfg = self.cfg
        pools = cfg.pools_used()
        log(f"бумажный режим: {len(cfg.strategies)} стратегий, опрос каждые {cfg.poll_seconds} с, "
            f"состояние — {self.path}")
        n = 0
        try:
            while ticks is None or n < ticks:
                try:
                    snap = read_pools(pools)
                except Exception as e:  # noqa: BLE001 — сеть и лимиты не должны останавливать бота
                    log(f"не удалось прочитать пулы: {e}")
                else:
                    self.tick(snap)
                n += 1
                if ticks is None or n < ticks:
                    time.sleep(cfg.poll_seconds)
        except KeyboardInterrupt:
            log("остановлен, состояние сохранено")
        self.show()

    def show(self):
        """Отчёт по текущему состоянию на свежих ценах."""
        if not self.books:
            print("позиций ещё нет — запустите бумажный режим: python3 bot.py paper")
            return
        print(report.table(self.rows(read_pools(self.cfg.pools_used())), self.cfg.staking_apy))
