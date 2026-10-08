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

import csv
import json
import threading
from html import escape as esc
import math
import os
import subprocess
import time
import traceback

import requests
from dataclasses import asdict, replace
from datetime import datetime, timezone

from lpscan.common import ROOT
from sui_pools import SUI, USDC
from suibot import charts, history, notify
from suibot.book import (Book, accrue_growth, close_to_idle, decide, exit_to_sui, exit_to_usdc, init_book,
                         open_position, rebalance, summary)
from suibot.chain import raw_to_usd, read_pools, state_from_price, token_price, usd_to_raw
from suibot.clmm import amounts, snap_ticks, sqrt_of_tick
from suibot.config import Config
from suibot.paper import _append, _utc, log
from suibot.rally import RallyWatch
from suibot.sim import simulate

try:
    import fcntl
except ImportError:            # Windows
    fcntl = None
    import msvcrt

GAS_BUFFER_SUI = 0.05      # при открытии позиции столько своих SUI бот оставляет на газ
RETRY_MINUTES = (2, 6, 18)  # повторы после ошибки; следующая ошибка подряд — пауза до /resume
SYNC_MINUTES = 30          # плановая сверка с кошельком
COMMANDS = ("status", "pause", "resume", "sui", "usdc", "close", "events", "week", "alert", "strategy", "model",
            "trend", "settings", "help")
HELP = ("Команды: /status — отчёт; /pause — пауза; /resume — продолжить; /sui — снять позицию и всё в SUI; "
        "/usdc — снять позицию и всё в USDC; /close — снять позицию, монеты оставить. После /sui, /usdc, /close "
        "бот на паузе; /resume — снова открыть позицию.")
MANUAL = {"sui": "вручную: всё в SUI", "usdc": "вручную: всё в USDC", "close": "вручную: позиция снята"}
MENU = {"status": "отчёт: позиция, заработок, итог", "pause": "пауза (позиция остаётся)", "resume": "продолжить",
        "sui": "снять позицию, всё в SUI", "usdc": "снять позицию, всё в USDC", "close": "снять позицию",
        "events": "последние события", "week": "недельный отчёт с графиком", "alert": "алерт цены: /alert 1.30 (/alert — список, /alert off — снять)",
        "strategy": "проверить стратегии на свежих ценах", "model": "сверка: реальный бот против модели с запуска",
        "trend": "тренд SUI на масштабах 50/100/200/365 дней",
        "settings": "настройки бота с пояснениями",
        "help": "список команд"}
TX_URL = "https://suiscan.xyz/mainnet/tx/"
OBJ_URL = "https://suiscan.xyz/mainnet/object/"
TX_NAMES = {"open": "открытие", "close": "закрытие", "swap": "обмен"}
ICONS = {"открыта": "✅", "позиция найдена": "🔎", "позиция закрыта не ботом": "🛑", "пауза": "⏸", "продолжение": "▶️",
         "вручную: всё в SUI": "✋", "вручную: всё в USDC": "✋", "вручную: позиция снята": "✋", "выход завершён": "☑️",
         "пересборка": "🔄", "позиция открыта заново": "🔄", "возврат в пул": "↩️", "выход в SUI": "🚀",
         "выход в USDC": "🛡", "цена вне диапазона": "⚠️", "цена снова в диапазоне": "✅", "нет связи": "📡",
         "ошибка": "❌", "мало SUI на газ": "⛽", "награды обменяны": "🎁", "обмен наград не удался": "🎁",
         "бот был выключен": "💤", "реинвестирование": "♻️", "цена пула расходится с биржей": "🚧",
         "отставание от «держать SUI»": "📉", "связь восстановлена": "📡", "переход в другой пул": "🔀",
         "выход в SUI пропущен": "🧭", "выход в USDC пропущен": "🧭"}
BINANCE_PRICE = "https://data-api.binance.vision/api/v3/ticker/price"


def exchange_price() -> float | None:
    """Цена SUI на Binance (SUIUSDT) для сверки с ценой пула; None — биржа недоступна."""
    try:
        return float(requests.get(BINANCE_PRICE, params={"symbol": "SUIUSDT"}, timeout=8).json()["price"])
    except (requests.RequestException, ValueError, KeyError, TypeError):
        return None


def rules_text(rules) -> str:
    """[[72, 0.15]] → «≥15% за 72 ч»."""
    return ", ".join(f"≥{r:.0%} за {h:g} ч" for h, r in rules or [])


def money(x: float, sign: bool = False) -> str:
    s = f"${abs(x):,.2f}"
    return ("+" if x >= 0 else "−") + s if sign else ("−" + s if x < 0 else s)


def pct(x: float) -> str:
    return f"{'+' if x >= 0 else '−'}{abs(x):.1%}"


def num(x: float, d: int = 1) -> str:
    return f"{'+' if x >= 0 else '−'}{abs(x):,.{d}f}"


def age(days: float) -> str:
    m = days * 1440
    return f"{m:.0f} мин" if m < 60 else f"{m / 60:.1f} ч" if m < 1440 else f"{days:.1f} дн."


def bar(p: float, lo: float, hi: float, n: int = 12) -> str:
    """Где цена внутри диапазона: ┃───●────┃ (точка снаружи — цена вне диапазона)."""
    if p < lo:
        return "●┃" + "─" * n + "┃"
    if p > hi:
        return "┃" + "─" * n + "┃●"
    i = min(n - 1, int((p - lo) / (hi - lo) * n))
    return "┃" + "─" * i + "●" + "─" * (n - 1 - i) + "┃"


def lock_once(path):
    """Один экземпляр бота на папку: второй запуск получает OSError."""
    f = path.open("a+")
    try:
        if fcntl:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        else:
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        f.close()
        raise
    return f


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


def scales_lines(closes: list, bot_days: float | None = None, windows=(50, 100, 200, 365)) -> list[str]:
    """Картина рынка по дневным ценам [(время, цена)]: максимум и дно после него, цена против средних за 50/100/200/365
    дней (выше/ниже, растёт ли средняя за месяц, когда цена последний раз пробила её)."""
    if len(closes) < 2:
        return ["🧭 Тренд по масштабам: нет дневных цен (Binance недоступен) — попробуйте /trend позже"]
    t = [x[0] for x in closes]
    p = [x[1] for x in closes]
    day = lambda i: datetime.fromtimestamp(t[i], timezone.utc).strftime("%d.%m.%y")   # noqa: E731
    hi = max(range(len(p)), key=p.__getitem__)
    lo = min(range(hi, len(p)), key=p.__getitem__)
    now = p[-1]
    lines = ["🧭 <b>Тренд SUI по масштабам</b> (дневные цены Binance)",
             f"сейчас ${now:.4f} · от максимума ${p[hi]:.2f} ({day(hi)}) {pct(now / p[hi] - 1)}"
             + (f" · от дна ${p[lo]:.3f} ({day(lo)}) {pct(now / p[lo] - 1)}" if lo != hi else ""), ""]

    def ma(n, i):
        return sum(p[i - n + 1:i + 1]) / n

    ups = 0
    for n in windows:
        if len(p) < n + 30:
            lines.append(f"{n} дн.: мало истории")
            continue
        m, m_ago = ma(n, len(p) - 1), ma(n, len(p) - 31)
        cross = next((i for i in range(len(p) - 1, n, -1) if (p[i] >= ma(n, i)) != (p[i - 1] >= ma(n, i - 1))), None)
        above, rising = now >= m, m > m_ago
        ups += above and rising
        lines.append(f"{'🟢' if above and rising else '🔴' if not above and not rising else '🟡'} <b>{n} дн.</b>: средняя "
                     f"${m:.3f} ({'растёт' if rising else 'падает'}), цена {'выше' if above else 'ниже'} "
                     f"({pct(now / m - 1)})" + (f" · пробой {'вверх' if above else 'вниз'} {day(cross)}" if cross else "")
                     + (" ← фильтр бота" if bot_days and n == bot_days else ""))
    n_ok = sum(1 for n in windows if len(p) >= n + 30)
    long = windows[-1]
    lines += ["", f"растущий тренд на {ups} из {n_ok} масштабов"
              + (f"; годовой разворот подтвердится, когда цена закрепится выше ${ma(long, len(p) - 1):.3f} и годовая "
                 "средняя начнёт расти" if len(p) >= long + 30 and not (now >= ma(long, len(p) - 1)
                                                                       and ma(long, len(p) - 1) > ma(long, len(p) - 31))
                 else ""),
              "🧠 <i>почему: это картина рынка для ваших решений (держать SUI, увеличивать ли сумму); на сделки бота "
              + (f"влияет только средняя за trend_ma_days = {bot_days:g} дн." if bot_days else "она не влияет")
              + " — 🟢 цена выше растущей средней, 🔴 ниже падающей, 🟡 смешанно</i>"]
    return lines


def calibration_lines(real: dict, earned: float, model: dict, manual: int = 0) -> list[str]:
    """Текст сверки: real — summary реального бота, earned — его комиссии и награды в $, model — summary
    симулятора за тот же период с тем же капиталом."""
    days, got = real["days"], model["fees_usd"]
    ratio = earned / got if got > 0 else None
    g_real = real["value_sui"] / real["capital_sui"] - 1
    g_model = model["value_sui"] / model["capital_sui"] - 1
    lines = [f"🔬 <b>Факт против модели</b> · с запуска, {age(days)}",
             "<i>модель — тот же симулятор, по которому выбиралась стратегия, на реальных ценах этих дней</i>", "",
             f"заработано (комиссии + награды): факт <b>{money(earned)}</b> · модель {money(got)}"
             + (f" → <b>{ratio:.0%}</b> от модели" if ratio is not None else ""),
             f"штук SUI с запуска: факт {pct(g_real)} · модель {pct(g_model)}",
             f"пересборок: факт {real['rebalances']} · модель {model['rebalances']}; выходов в SUI/USDC: "
             f"факт {real['exits'] + real['crashes']} · модель {model['exits'] + model['crashes']}",
             f"издержки (обмены и газ): факт {money(real['costs_usd'])} · модель {money(model['costs_usd'])}"]
    if not (real["exits"] + real["crashes"] + model["exits"] + model["crashes"]):
        lines.append(f"в диапазоне: факт {real['in_range_pct']:.0f}% · модель {model['in_range_pct']:.0f}% времени")
    if manual:
        lines.append(f"✋ ручных команд и пауз с запуска: {manual} — модель их не делает, это часть разницы")
    if days < 3 or ratio is None:
        verdict = "⏳ данных пока мало — первые выводы после недели работы"
    elif ratio < 0.7:
        verdict = ("⚠️ реальный доход заметно ниже модели — бэктесты завышают результат; сумму не увеличивать, "
                   "журнал — на разбор")
    elif ratio > 1.3:
        verdict = "📈 реальный доход выше модели — модель скорее осторожна"
    else:
        verdict = "✅ модель подтверждается: реальный доход в пределах ±30% от расчёта"
    return lines + ["", verdict, "🧠 <i>почему: стратегию и сумму выбирали по этому симулятору; сверка показывает, "
                                 "насколько ему можно верить на реальных деньгах. Раз в неделю — вместе с недельным "
                                 "отчётом, по запросу — /model</i>"]


class Live:
    def __init__(self, cfg: Config):
        if not cfg.live:
            raise SystemExit("в suibot.toml нет раздела [live]")
        self.cfg, self.lc = cfg, cfg.live
        self.s = next(s for s in cfg.strategies if s.name == self.lc.strategy)
        self.pc = cfg.pools[self.s.pool]                # пул исполнения; при смене пула — пока пул книги (ниже)
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
        if self.book and self.book.pool != self.s.pool and self.book.pool in cfg.pools:
            self.pc = cfg.pools[self.book.pool]         # деньги ещё в старом пуле — бот работает там до /close
        self.switch_noted = st.get("switch_noted", False)
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
        self.watch = RallyWatch(self.s.rally_exit, st.get("watch"), min_step=60, drop_rules=self.s.crash_exit,
                                trend_days=self.s.trend_ma_days)
        self.trend_try = 0.0                              # последняя попытка загрузить предысторию средней
        self.skip_noted = None                            # о пропущенном выходе уже сообщено
        self.out_noticed = st.get("out_noticed", False)   # сообщение «цена вне диапазона» уже отправлено
        self.last_report_t = st.get("last_report_t")      # когда отправлен последний регулярный отчёт (часы Mac)
        self.txs = []                                     # транзакции текущего действия — ссылки в сообщение
        self.alerts = st.get("alerts", [])                # ценовые алерты: [[цена, "up"|"down"], ...]
        self.day_snap = st.get("day_snap")                # итоги на момент прошлой утренней сводки
        self.gas_warned = st.get("gas_warned", False)
        self.last_tick_t = st.get("last_tick_t")          # часы Mac на последнем опросе — для «бот был выключен»
        self.down_since = None
        self.last_reinvest = st.get("last_reinvest")
        self.onchain = st.get("onchain")                  # несобранные комиссии и награды по данным сети
        self.lag_hist = st.get("lag_hist", [])             # [время, SUI к «держать SUI»] на каждое утро
        self.price_warned = False
        self.net_down_since = None
        self.last_ping = 0.0
        self.last_snap_t = st.get("last_snap_t", 0.0)
        self.last_strategy_check = st.get("last_strategy_check")
        self.hello = False
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
            "last_sync": self.last_sync, "watch": self.watch.dump(), "out_noticed": self.out_noticed,
            "last_report_t": self.last_report_t, "alerts": self.alerts, "day_snap": self.day_snap,
            "gas_warned": self.gas_warned, "last_tick_t": self.last_tick_t, "last_reinvest": self.last_reinvest,
            "onchain": self.onchain, "lag_hist": self.lag_hist, "last_snap_t": self.last_snap_t,
            "last_strategy_check": self.last_strategy_check, "switch_noted": self.switch_noted}, ensure_ascii=False))
        tmp.replace(self.path)

    def event(self, t: float, kind: str, price: float, text: str, st=None, why: str | None = None):
        """Событие: журнал и events.csv — одной строкой; Telegram — карточкой (части текста через «; » — строки,
        st — добавить итог капитала, why — почему бот так сделал и какой настройкой это меняется,
        транзакции этого действия — ссылками)."""
        links, self.txs = self.txs, []
        plain = text + (f"; {self.report(st).splitlines()[1]}" if st and self.book and self.book.start else "")
        if why:
            plain += f" | почему: {why}"
        if links:
            plain += "\nтранзакции: " + " ".join(TX_URL + d for _, d in links)
        log(f"[{self.label}] {kind}: {plain}")
        _append(self.dir / "events.csv", {"time_utc": _utc(t), "mode": self.label, "event": kind,
                                          "price": round(price, 5), "details": plain})
        notify.send(self.card(kind, text, st, links, why), html=True)

    def card(self, kind: str, text: str, st=None, links=(), why: str | None = None) -> str:
        lines = [f"{ICONS.get(kind, 'ℹ️')} <b>{esc(kind[:1].upper() + kind[1:])}</b>" + (" · 🧪 симуляция" if self.dry else "")]
        lines += [esc(x.strip()) for x in text.split("; ") if x.strip()]
        if st and self.book and self.book.start:
            r = summary(self.book, st, self.price_of(st))
            cap = self.book.start["capital_sui"] * self.book.start["price"]
            lines.append(f"💼 <b>{money(r['value'])}</b> = {r['value_sui']:,.1f} SUI"
                         + (f" · итог {pct(r['value'] / cap - 1)}" if cap else ""))
        if why:
            lines.append(f"🧠 <i>почему: {esc(why)}</i>")
        if links:
            lines.append("🔗 " + " · ".join(f'<a href="{TX_URL}{d}">{TX_NAMES.get(op, op)}</a>' for op, d in links))
        return "\n".join(lines)

    def why(self, kind: str, mode: str | None = None) -> str:
        """Логика решения простыми словами и настройки suibot.toml, которыми она меняется."""
        s, lc = self.s, self.lc

        def trend_note(side: str) -> str:
            ma = self.watch.trend_ma() if s.trend_ma_days else None
            if ma is None:
                return ""
            return (f"; фильтр тренда: цена {'выше' if side == 'up' else 'ниже'} средней за trend_ma_days = "
                    f"{s.trend_ma_days:g} дн. (${ma:.4f}) — выход по направлению рынка")
        rng = f"−{s.range_down:.0%}…+{s.range_up:.0%} от цены (range_down / range_up)"
        return {
            "открыта": f"бот берёт из кошелька не больше max_capital_usd = ${lc.max_capital_usd:g} и оставляет "
                       f"gas_reserve_sui = {lc.gas_reserve_sui:g} SUI на газ; диапазон {rng} — узкий диапазон даёт больше "
                       "комиссий, пока цена внутри",
            "пересборка": f"цена была вне диапазона дольше out_minutes = {s.out_minutes:g} мин, комиссии не шли — бот "
                          f"поставил новый диапазон {rng}; короткие выбросы он пережидает, потому что каждая "
                          "пересборка стоит комиссии обмена и газа",
            "позиция открыта заново": "позиции бота нет в кошельке (не открылась или пропала) — без позиции нет "
                                      f"комиссий, поэтому бот открывает новую: {rng}",
            "выход в SUI": f"сильный рост (rally_exit: {rules_text(s.rally_exit)}) — в пуле рост продаёт ваши SUI за "
                           "USDC, поэтому бот держит всё в SUI; вернётся в пул после отката на "
                           f"resume_drop_pct = {s.resume_drop_pct or 0:.0%} от пика" + trend_note("up"),
            "выход в USDC": f"сильное падение (crash_exit: {rules_text(s.crash_exit)}) — в пуле падение докупает SUI "
                            "всё дороже, поэтому бот держит всё в USDC; вернётся в пул после отскока на "
                            f"resume_rise_pct = {s.resume_rise_pct or 0:.0%} от минимума" + trend_note("down"),
            "выход в SUI пропущен": f"сильный рост ({rules_text(s.rally_exit)}), но цена ниже средней за trend_ma_days = "
                                    f"{s.trend_ma_days or 0:g} дн. — рынок в целом падает, и такой рост чаще оказывается "
                                    "отскоком: по истории выходы в SUI на отскоках теряли 3–8% штук SUI; бот остаётся в "
                                    "пуле и продолжает собирать комиссии",
            "выход в USDC пропущен": f"сильное падение ({rules_text(s.crash_exit)}), но цена выше средней за "
                                     f"trend_ma_days = {s.trend_ma_days or 0:g} дн. — рынок в целом растёт, и такие "
                                     "провалы чаще выкупают: по истории выходы в USDC на них теряли 2–6% штук SUI; бот "
                                     "остаётся в пуле и продолжает собирать комиссии",
            "возврат в пул": ("после выхода в SUI цена откатилась на resume_drop_pct = "
                              f"{s.resume_drop_pct or 0:.0%} от пика — сильный рост закончился" if mode == "sui" else
                              "после выхода в USDC цена отскочила на resume_rise_pct = "
                              f"{s.resume_rise_pct or 0:.0%} от минимума — падение остановилось" if mode == "usdc" else
                              "после вашей команды /resume") + f"; бот снова зарабатывает комиссии: {rng}",
            "цена вне диапазона": "комиссии платят только пока цена внутри диапазона; бот ждёт out_minutes = "
                                  f"{s.out_minutes:g} мин, прежде чем переставлять: короткие выбросы часто "
                                  "возвращаются, а пересборка стоит денег",
            "ошибка": "после ошибки бот сверяется с кошельком (чтобы не потратить дважды) и повторяет через 2, 6 и 18 "
                      "мин; 4-я ошибка подряд — пауза, чтобы не тратить газ на повторяющийся сбой",
            "нет связи": "без связи бот ничего не отправляет, деньги не трогаются; повторяет сам и на паузу не встаёт",
            "мало SUI на газ": f"каждая транзакция платит газ в SUI; без газа бот не сможет ни переставить, ни закрыть "
                               f"позицию; порог gas_warn_sui = {lc.gas_warn_sui:g} SUI",
            "реинвестирование": f"раз в reinvest_days = {lc.reinvest_days:g} дн. бот забирает заработанное, меняет "
                                f"награды дороже reward_min_usd = ${lc.reward_min_usd:g} на SUI (цель — больше SUI) и "
                                "добавляет всё в позицию: деньги в позиции зарабатывают комиссии, а лежащие рядом — нет; "
                                f"меньше reinvest_min_usd = ${lc.reinvest_min_usd:g} не добавляется — копится",
            "обмен наград не удался": f"попробует снова через reinvest_days = {lc.reinvest_days:g} дн.; "
                                      "на позицию это не влияет",
            "цена пула расходится с биржей": f"разница больше price_check_pct = {lc.price_check_pct:.0%}: это может быть "
                                             "короткий выброс в пуле или сбой узла; действовать по такой цене опасно, "
                                             "поэтому бот ждёт, пока цены сойдутся, и проверяет каждые 30 с",
            "отставание от «держать SUI»": f"порог lag_warn_pct = {lc.lag_warn_pct:.0%} за 7 дней; бот сам ничего не "
                                           "меняет — пауза отключила бы и защиту от падения; решать вам: продолжать, "
                                           "/sui (переждать в SUI) или обсудить стратегию",
            "связь восстановлена": "пока связи нет, бот ничего не делает и деньги не трогает; позиция в пуле "
                                   "продолжает работать сама",
            "бот был выключен": "пока бот выключен, он не переставляет позицию и не выходит на сильных движениях; "
                                "держите Mac включённым или перенесите бота на сервер",
            "переход в другой пул": "в [live] strategy выбрана стратегия в другом пуле; деньги бота ещё в старом пуле, "
                                    "поэтому бот продолжает работать там по новым правилам, а переезжает только по "
                                    "вашей команде: /close снимает позицию, /resume открывает её уже в новом пуле "
                                    f"(не больше max_capital_usd = ${lc.max_capital_usd:g})",
        }.get(kind, "")

    def buttons(self) -> list:
        return [[("🔄 Обновить", "status"), ("▶️ Продолжить", "resume") if self.paused else ("⏸ Пауза", "pause"),
                 ("📜 События", "events")]]

    def earned(self, st, r) -> float:
        """Комиссии и награды в $ с начала."""
        return r["fees_usd"] + r["value"] - self.usd(*self.book.holdings(st), st)

    def snap(self, st, r) -> dict:
        b = self.book
        return {"t": time.time(), "earned": self.earned(st, r), "in_s": b.in_range_s, "tot_s": b.total_s,
                "acts": len(b.rebalances) + len(b.exits) + len(b.crashes) + len(b.resumes),
                "vs_hold": r["vs_hold_sui_count"], "price": r["price"]}

    def daily(self, st, r) -> list[str]:
        """Сводка с прошлой утренней сводки (или с запуска)."""
        b, p = self.book, r["price"]
        d0 = self.day_snap or {"t": None, "earned": 0.0, "in_s": 0.0, "tot_s": 0.0, "acts": 0, "vs_hold": 0.0,
                               "price": b.start["price"]}
        now = self.snap(st, r)
        tot = now["tot_s"] - d0["tot_s"]
        got = now["earned"] - d0["earned"]
        out = ["", "🗓 <b>" + ("За сутки" if d0["t"] else "С запуска") + "</b>",
               f"заработано <b>{money(got)}</b> ≈ {got / p:,.2f} SUI",
               f"цена SUI {pct(p / d0['price'] - 1)}"
               + (f" · в диапазоне {(now['in_s'] - d0['in_s']) / tot:.0%} времени" if tot > 0 else ""),
               f"действий бота: {now['acts'] - d0['acts']} · к «держать SUI» {num(now['vs_hold'] - d0['vs_hold'], 2)} SUI"]
        self.day_snap = now
        self.lag_hist = (self.lag_hist + [[now["t"], now["vs_hold"]]])[-60:]
        old = [h for h in self.lag_hist if now["t"] - h[0] >= 6.5 * 86400]
        if old and self.lc.lag_warn_pct:
            lag = (now["vs_hold"] - old[-1][1]) / b.start["capital_sui"]
            if lag < -self.lc.lag_warn_pct:
                self.event(st["t"], "отставание от «держать SUI»", p, f"за 7 дней бот отстал от «держать SUI» на "
                           f"{-lag:.1%} ({num(now['vs_hold'] - old[-1][1])} SUI)", st,
                           why=self.why("отставание от «держать SUI»"))
        return out

    def onchain_line(self, st) -> str:
        """Несобранное в позиции по данным сети (обновляется при сверке раз в 30 мин)."""
        o = self.onchain
        fee = self.usd(int(o.get("fee_a") or 0), int(o.get("fee_b") or 0), st)
        rew = 0.0
        for t, amt in (o.get("rewards") or {}).items():
            px, dec = self.price_of(st)(t)
            rew += int(amt) * px / 10 ** dec
        return f"в позиции сейчас (точно): комиссии {money(fee)} + награды {money(rew)}"

    def settings_card(self) -> str:
        s, lc = self.s, self.lc
        rows = [("Стратегия", f"«{s.name}»", "[live] strategy — какая из стратегий suibot.toml работает"),
                ("Диапазон", f"−{s.range_down:.0%}…+{s.range_up:.0%}", "range_down / range_up — уже: больше комиссий, "
                 "но чаще пересборки"),
                ("Пересборка", f"после {s.out_minutes:g} мин вне диапазона", "out_minutes — меньше: быстрее "
                 "возвращается к комиссиям, но больше лишних пересборок на выбросах"),
                ("Выход в SUI", rules_text(s.rally_exit) or "нет", f"rally_exit; назад в пул после отката "
                 f"resume_drop_pct = {s.resume_drop_pct or 0:.0%}"),
                ("Выход в USDC", rules_text(s.crash_exit) or "нет", f"crash_exit; назад после отскока "
                 f"resume_rise_pct = {s.resume_rise_pct or 0:.0%}"),
                ("Фильтр тренда", f"средняя за {s.trend_ma_days:g} дн." if s.trend_ma_days else "выключен",
                 "trend_ma_days — выход в SUI только выше средней, в USDC — только ниже: отскоки и провалы против "
                 "тренда бот пережидает в пуле"),
                ("Лимит капитала", f"${lc.max_capital_usd:g}", "max_capital_usd — больше бот из кошелька не берёт"),
                ("Газ", f"{lc.gas_reserve_sui:g} SUI в запасе, тревога ниже {lc.gas_warn_sui:g}",
                 "gas_reserve_sui / gas_warn_sui"),
                ("Обмен", f"проскальзывание до {lc.slippage:.1%}", "slippage — больше: обмен проходит чаще, но может "
                 "быть дороже"),
                ("Реинвестирование", f"раз в {lc.reinvest_days:g} дн." if lc.reinvest_days else "выключено",
                 f"reinvest_days; награды от ${lc.reward_min_usd:g}, добавка от ${lc.reinvest_min_usd:g}"),
                ("Сверка с Binance", f"±{lc.price_check_pct:.0%}", "price_check_pct — защита от ложной цены"),
                ("Отчёты", f"каждые {lc.report_hours:g} ч + утро", "report_hours"),
                ("Проверка стратегий", f"раз в {lc.strategy_check_days:g} дн.", "strategy_check_days"),
                ("Режим", "🧪 симуляция" if self.dry else "реальные деньги", "dry_run")]
        return "⚙️ <b>Настройки</b> (файл suibot.toml)\n" + "\n".join(
            f"\n<b>{esc(k)}</b>: {esc(v)}\n<i>{esc(d)}</i>" for k, v, d in rows)

    def report_card(self, st, title: str = "Отчёт", daily: bool = False) -> str:
        """Отчёт для Telegram: цена и диапазон, капитал, заработок (daily — и сводка за сутки)."""
        b = self.book
        lines = [f"📊 <b>{esc(title)}</b> · {esc(self.s.name)}"] + (["🧪 симуляция — реальных денег нет"] if self.dry else [])
        if not b or not b.start:
            return "\n".join(lines + ["позиция ещё не открыта" + (" (пауза)" if self.paused else "")])
        r = summary(b, st, self.price_of(st))
        p, (lo, hi) = r["price"], r["range"]
        state = {"sui": "🚀 всё в SUI — ждёт отката, чтобы вернуться в пул",
                 "usdc": "🛡 всё в USDC — ждёт отскока, чтобы вернуться в пул"}.get(r["mode"]) or (
            "без позиции" if not b.L else "✅ в диапазоне — комиссии идут" if r["in_range_now"]
            else "⚠️ вне диапазона — комиссии не идут")
        lines += ["", f"💲 SUI <b>${p:.4f}</b>", ("⏸ пауза · " if self.paused else "") + state]
        if b.mode == "lp" and b.L:
            lines += [f"<code>{lo:.4f} {bar(p, lo, hi)} {hi:.4f}</code>",
                      f"до нижней {pct(lo / p - 1)} · до верхней {pct(hi / p - 1)} · "
                      f"в диапазоне {r['in_range_pct']:.0f}% времени"]
        if self.s.trend_ma_days:
            ma = self.watch.trend_ma()
            lines.append(f"🧭 тренд {self.s.trend_ma_days:g} дн.: " + (
                "копится история — пока выходы без фильтра" if ma is None else
                f"цена {'выше' if p >= ma else 'ниже'} средней ${ma:.4f} — разрешён выход "
                + ("в SUI на росте" if p >= ma else "в USDC на падении")))
        cap = b.start["capital_sui"] * b.start["price"]
        pnl = r["value"] - cap
        lines += ["", "💼 <b>Капитал</b>",
                  f"{money(cap)} → <b>{money(r['value'])}</b> ({money(pnl, True)}" + (f", {pct(pnl / cap)})" if cap else ")"),
                  f"= {r['value_sui']:,.1f} SUI · к «держать SUI» {num(r['vs_hold_sui_count'])} SUI",
                  f"состав: {r['sui_amount']:,.1f} SUI + {r['usdc_amount']:,.2f} USDC"]
        rew = r["value"] - self.usd(*b.holdings(st), st)
        earned = self.earned(st, r)
        day = (f" · ≈{money(earned / r['days'])} в день (≈{earned / r['days'] * 365 / cap:.0%} годовых)"
               if r["days"] >= 1 / 24 and cap else "")
        lines += ["", "💰 <b>Заработок</b>",
                  f"комиссии ≈{money(r['fees_usd'])} · награды {money(rew)}",
                  *([self.onchain_line(st)] if self.onchain and self.pos_id and not self.dry else []),
                  f"итого <b>{money(earned)}</b> ≈ {earned / p:,.2f} SUI{day}",
                  f"издержки {money(r['costs_usd'])} · пересборок {r['rebalances']}"]
        foot = f"⏱ работает {age(r['days'])}"
        if self.pos_id:
            foot = f'🔗 <a href="{OBJ_URL}{self.pos_id}">позиция {self.pos_id[:10]}…</a> · ' + foot
        if self.errors:
            foot += f" · ❌ ошибок подряд: {self.errors}"
        if self.alerts:
            foot += " · 🔔 алерты: " + ", ".join(f"${x:g}" for x, _ in self.alerts)
        return "\n".join(lines + (self.daily(st, r) if daily else []) + ["", foot])

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
                f"издержки ${r['costs_usd']:,.2f}, пересборок {r['rebalances']}, дней {r['days']:.1f}\n"
                + self.details(st, r))

    def details(self, st, r) -> str:
        """Позиция и заработок — для /status и регулярных отчётов (строки после третьей)."""
        b, p = self.book, r["price"]
        lines = []
        if b.mode == "lp" and b.L:
            lo, hi = r["range"]
            where = f"позиция {self.pos_id[:10]}…" if self.pos_id else "позиция (виртуальная)"
            lines.append(f"{where}: до нижней границы {lo / p - 1:+.1%}, до верхней {hi / p - 1:+.1%}; "
                         f"в диапазоне {r['in_range_pct']:.0f}% времени")
        rew = r["value"] - self.usd(*b.holdings(st), st)
        earned = r["fees_usd"] + rew
        cap = b.start["capital_sui"] * b.start["price"]
        line = f"заработок: комиссии ≈${r['fees_usd']:,.2f} + награды ${rew:,.2f} = ${earned:,.2f} (≈{earned / p:,.1f} SUI)"
        if r["days"] >= 1 / 24 and cap:
            line += f", ≈${earned / r['days']:,.2f} в день (≈{earned / r['days'] * 365 / cap:.0%} годовых)"
        lines.append(line)
        if cap:
            pnl = r["value"] - cap
            lines.append(f"итог: ${cap:,.2f} → ${r['value']:,.2f} ({pnl:+,.2f}$, {pnl / cap:+.1%}); "
                         f"к «держать SUI» {r['vs_hold_sui_count']:+,.1f} SUI")
        return "\n".join(lines)

    # --- реальные транзакции -----------------------------------------------------------------------------
    def ex(self, st, op: dict, *args) -> dict:
        """Реальная транзакция: симуляция, затем подпись и отправка. Пока ответа нет, в состоянии записано, что
        отправлялось и сколько было в кошельке перед отправкой (pending) — если ответ потеряется, сверка с
        кошельком посчитает точную разницу. Снимается pending в apply_changes, когда изменения учтены."""
        sim = executor(*args, simulate=True)
        self.pending = {**op, "before": sim.get("wallet")}
        self.save()
        try:
            res = executor(*args, simulate=False)
        except ExecError as e:
            if e.out.get("sent") is False:                 # до сети не дошло — ничего не изменилось
                self.pending = None
            elif e.out.get("digest") and e.out.get("status"):   # исполнилась с ошибкой: списан только газ
                self.txs.append((op.get("op"), e.out["digest"]))
                self.apply_changes(e.out, st)
            raise
        if res.get("digest"):
            self.txs.append((op.get("op"), res["digest"]))
        return res

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
                notify.send(f"ℹ️ <b>В кошельке новая позиция</b>\n{p['id'][:10]}… — бот её не трогает и не учитывает", html=True)
        self.gas_check(w, st)
        avail_a, avail_b = self.available(w)
        if self.pos_id and w.get("position") and "fee_a" in w["position"]:
            self.onchain = {"t": time.time(), "fee_a": w["position"].get("fee_a"), "fee_b": w["position"].get("fee_b"),
                            "rewards": w["position"].get("rewards") or {}}
        if pend and pend.get("op") == "collect":           # оценка комиссий заменяется фактом из разницы балансов
            b.fees_a = b.fees_b = 0.0
            b.rewards = dict(self.collected)
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

    def price_ok(self, st) -> bool:
        """Цена пула не расходится с Binance больше чем на price_check_pct. Биржа недоступна — не мешаем."""
        ex = exchange_price()
        if ex is None or not self.lc.price_check_pct:
            return True
        dev = st["sui"] / ex - 1
        if abs(dev) <= self.lc.price_check_pct:
            self.price_warned = False
            return True
        if not self.price_warned:
            self.price_warned = True
            self.event(st["t"], "цена пула расходится с биржей", st["sui"], f"пул ${st['sui']:.4f}, Binance ${ex:.4f} "
                       f"({pct(dev)}); действие отложено", why=self.why("цена пула расходится с биржей"))
        return False

    def gas_check(self, w: dict, st):
        """Предупредить один раз, если SUI в кошельке на газ меньше gas_warn_sui (снова — после пополнения)."""
        bal = {norm(k): int(v) for k, v in (w.get("balances") or {}).items()}
        sui = bal.get(norm(SUI), 0) / 1e9
        if sui < self.lc.gas_warn_sui and not self.gas_warned:
            self.gas_warned = True
            self.event(st["t"], "мало SUI на газ", st["sui"], f"в кошельке {sui:.3f} SUI; пополните кошелёк бота на "
                       "1–2 SUI", why=self.why("мало SUI на газ"))
        elif sui >= self.lc.gas_warn_sui + 0.2:
            self.gas_warned = False

    # --- старт -------------------------------------------------------------------------------------------
    def start_real(self, st):
        if not self.price_ok(st):
            return
        w = self.status()
        self.address = w["address"]
        self.foreign = {p["id"] for p in w.get("positions", [])}
        if self.foreign:
            notify.send(f"ℹ️ <b>В кошельке уже есть позиции в этом пуле: {len(self.foreign)}</b>\nбот их не трогает", html=True)
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
        self.event(st["t"], "открыта", st["sui"], f"кошелёк {w['address'][:10]}…; капитал ${capital:,.2f}; "
                   f"диапазон {lo:.4f}–{hi:.4f}; позиция {self.pos_id[:10]}…", why=self.why("открыта"))

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
        self.event(st["t"], "открыта", st["sui"], text, why=self.why("открыта"))

    # --- команды и решения -------------------------------------------------------------------------------
    def read_commands(self) -> list[str]:
        cmds, self.tg_offset = notify.commands(self.tg_offset)
        ctl = self.dir / "control.txt"
        if ctl.exists():
            cmds += [x.strip().lower() for x in ctl.read_text().splitlines() if x.strip()]
            ctl.unlink()
        return cmds

    def command(self, c: str, st):
        c, _, arg = c.partition(" ")
        if c == "events":
            notify.send(self.events_card(), html=True)
        elif c == "alert":
            self.alert_command(arg.strip(), st["sui"])
        elif c == "week":
            self.weekly(st)
        elif c == "settings":
            notify.send(self.settings_card(), html=True)
        elif c == "strategy":
            notify.send("🧪 Проверяю стратегии на ценах за 30 и 90 дней — это займёт пару минут")
            self.strategy_check()
        elif c == "model":
            if self.book and self.book.start:
                notify.send("🔬 Считаю модель за время работы бота — это займёт пару минут")
            self.calibration(st)
        elif c == "trend":
            self.trend_report()
        elif c not in COMMANDS or c == "help":
            notify.send("ℹ️ <b>Команды</b>\n" + "\n".join(f"/{k} — {esc(v)}" for k, v in MENU.items())
                        + "\n\nПосле /sui, /usdc, /close бот на паузе; /resume — снова открыть позицию.", html=True)
        elif c == "status":
            notify.send(self.report_card(st, "Статус"), html=True, buttons=self.buttons())
            log(self.report(st))
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
            notify.send("ℹ️ Позиция ещё не открыта")
        else:
            self.manual, self.paused = c, True         # выполнится в step и будет доведено до конца при сбое

    def events_card(self, n: int = 8) -> str:
        """Последние события из events.csv: время по часам Mac, иконка, суть."""
        path = self.dir / "events.csv"
        rows = list(csv.DictReader(path.open())) if path.exists() else []
        lines = ["📜 <b>Последние события</b>"]
        for r in rows[-n:]:
            try:
                t = datetime.strptime(r["time_utc"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).astimezone()
                when = t.strftime("%d.%m %H:%M")
            except (KeyError, ValueError):
                when = r.get("time_utc", "")
            what = (r.get("details") or "").split(" | почему")[0].split("\n")[0]
            what = what if len(what) <= 90 else what[:89] + "…"
            sim = " 🧪" if r.get("mode") == "СИМУЛЯЦИЯ" else ""
            lines.append(f"<code>{esc(when)}</code> {ICONS.get(r.get('event', ''), 'ℹ️')} "
                         f"<b>{esc(r.get('event', ''))}</b>{sim}\n{esc(what)}")
        return "\n".join(lines) if rows else "📜 Событий пока нет"

    def alert_command(self, arg: str, price: float):
        """/alert 1.30 — сообщить, когда SUI дойдёт до цены; /alert — список; /alert off — снять все."""
        if arg in ("off", "clear", "stop", "0", "нет"):
            self.alerts = []
            notify.send("🔕 Алерты сняты")
            return
        if arg:
            try:
                x = float(arg.replace(",", ".").lstrip("$"))
            except ValueError:
                x = 0.0
            if not 0 < x < 1000:
                notify.send("Формат: /alert 1.30 — сообщу, когда SUI дойдёт до $1.30")
                return
            if len(self.alerts) >= 10:
                notify.send("Уже 10 алертов — снимите лишние: /alert off")
                return
            self.alerts.append([x, "up" if x > price else "down"])
            notify.send(f"🔔 Алерт: сообщу, когда SUI {'поднимется' if x > price else 'опустится'} до ${x:g} "
                        f"(сейчас ${price:.4f})")
            return
        notify.send("🔔 Алерты: " + (", ".join(f"${x:g} ({'вверх' if d == 'up' else 'вниз'})" for x, d in self.alerts)
                                     if self.alerts else "нет. Поставить: /alert 1.30"))

    def check_alerts(self, st):
        p, left = st["sui"], []
        for x, d in self.alerts:
            if (d == "up" and p >= x) or (d == "down" and p <= x):
                notify.send(f"🔔 <b>SUI ${p:.4f}</b>\nсработал ваш алерт: {'выше' if d == 'up' else 'ниже'} ${x:g}",
                            html=True)
            else:
                left.append([x, d])
        self.alerts = left

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
            self.event(st["t"], MANUAL[what], st["sui"], "бот на паузе до /resume", st)
        elif b.mode in ("sui", "usdc") and not self.dry:
            if self.pos_id:
                self.close_real(st)
            if self.wrong_coin_usd(st, b.mode) >= self.lc.min_swap_usd:
                self.to_coin(st, b.mode)
                self.event(st["t"], "выход завершён", st["sui"], f"все монеты бота — в {b.mode.upper()}", st)

    def act(self, st, kind: str, why: str):
        b, s, p, t = self.book, self.s, st["sui"], st["t"]
        old, was = list(b.range_usd), b.mode
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
        detail = f"{why}; диапазон {old[0]:.4f}–{old[1]:.4f} → {lo:.4f}–{hi:.4f}" if kind in ("rebalance", "reopen", "resume") else why
        self.out_noticed = False
        self.event(t, name, p, detail, st, why=self.why(name, was if why != "по команде" else None))

    def range_notice(self, st):
        """Сообщить, что цена вышла из диапазона (дольше range_notice_minutes) и что вернулась."""
        b = self.book
        if not b or b.mode != "lp" or not b.L or self.paused:
            return
        lo, hi = b.range_usd
        p = st["sui"]
        if b.out_since is not None and not self.out_noticed and st["t"] - b.out_since >= self.lc.range_notice_minutes * 60:
            self.out_noticed = True
            left = max(0.0, self.s.out_minutes - (st["t"] - b.out_since) / 60)
            self.event(st["t"], "цена вне диапазона", p, f"SUI ${p:.4f}, диапазон {lo:.4f}–{hi:.4f}; комиссии сейчас не "
                       f"идут; если цена не вернётся, пересборка примерно через {left:.0f} мин",
                       why=self.why("цена вне диапазона"))
        elif b.out_since is None and self.out_noticed:
            self.out_noticed = False
            self.event(st["t"], "цена снова в диапазоне", p, f"SUI ${p:.4f}, диапазон {lo:.4f}–{hi:.4f}; комиссии идут")

    def reinvest(self, st):
        """Раз в reinvest_days: забрать комиссии и награды (позиция остаётся), награды → SUI, всё своё вне позиции —
        в ту же позицию. Добавляется, только если цена в диапазоне и набралось не меньше reinvest_min_usd."""
        self.last_reinvest = time.time()
        b, parts = self.book, []
        if self.pos_id and b.mode == "lp":
            res = self.ex(st, {"op": "collect"}, "collect", "--pool", self.pc.object, "--position", self.pos_id)
            b.fees_a = b.fees_b = 0.0                          # оценка заменяется фактом
            b.rewards = dict(self.collected)
            before = self.usd(b.idle_a, b.idle_b, st)
            self.apply_changes(res, st)
            parts.append(f"забрано комиссий ≈${self.usd(b.idle_a, b.idle_b, st) - before:,.2f}")
            self.onchain = None
        parts += self.swap_rewards(st)
        free = self.usd(b.idle_a, b.idle_b, st)
        if (self.pos_id and b.mode == "lp" and b.in_range(st["sq"]) and free >= self.lc.reinvest_min_usd
                and self.price_ok(st)):
            self.add_real(st)
            parts.append(f"добавлено в позицию ≈${free - self.usd(b.idle_a, b.idle_b, st):,.2f}")
        elif free >= 0.01:
            parts.append(f"вне позиции осталось ${free:,.2f} — добавится в следующий раз или при пересборке")
        self.event(st["t"], "реинвестирование", st["sui"], "; ".join(parts) or "нечего реинвестировать", st,
                   why=self.why("реинвестирование"))

    def add_real(self, st):
        """Добавить свои монеты вне позиции в открытую позицию (те же потолки и запас на сдвиг цены, что при открытии)."""
        b = self.book
        shares = []
        for k in (1 - self.lc.price_band, 1.0, 1 + self.lc.price_band):
            a1, b1 = amounts(1.0, st["sq"] * math.sqrt(k), sqrt_of_tick(b.tick_lo), sqrt_of_tick(b.tick_hi))
            shares.append(a1 * st["ua"] / (a1 * st["ua"] + b1 * st["ub"]))
        self.to_share(st, min(shares))
        gas = GAS_BUFFER_SUI * 1e9
        amt_a = int(max(b.idle_a - (gas if b.a_is_sui else 0), 0))
        amt_b = int(max(b.idle_b - (0 if b.a_is_sui else gas), 0))
        if self.usd(amt_a, amt_b, st) < 1:
            return
        res = self.ex(st, {"op": "add"}, "add", "--pool", self.pc.object, "--position", self.pos_id,
                      "--amount-a", amt_a, "--amount-b", amt_b, "--band", self.lc.price_band)
        self.apply_changes(res, st)
        self.need_sync = True                                  # точная ликвидность позиции — со следующей сверки

    def swap_rewards(self, st) -> list[str]:
        """Собранные ботом награды (CETUS и т.п.) дороже reward_min_usd — в SUI. Меняется не больше, чем бот собрал
        сам (чужие монеты кошелька не трогаются). Сбой обмена не останавливает бота. Возвращает строки для отчёта."""
        out = []
        bal = {norm(k): int(v) for k, v in self.status()["balances"].items()}
        for t, amt in list(self.collected.items()):
            have = min(int(amt), bal.get(norm(t), 0))
            if have <= 0 or self.is_sui(t):
                continue
            px, dec = self.price_of(st)(t)
            usd = have * px / 10 ** dec
            if usd < self.lc.reward_min_usd:
                continue
            sym = t.split("::")[-1]
            sui_before = self.book.idle_b if not self.pc.a_is_sui else self.book.idle_a
            try:
                res = self.ex(st, {"op": "swap"}, "swap", "--from", t, "--to", SUI, "--amount", have,
                              "--slippage", self.lc.slippage)
            except (ExecError, subprocess.TimeoutExpired) as e:
                self.event(st["t"], "обмен наград не удался", st["sui"], f"{have / 10 ** dec:,.2f} {sym}: {e}",
                           why=self.why("обмен наград не удался"))
                continue
            self.apply_changes(res, st)
            got = ((self.book.idle_b if not self.pc.a_is_sui else self.book.idle_a) - sui_before) / 1e9
            out.append(f"{have / 10 ** dec:,.2f} {sym} (≈${usd:,.2f}) → {got:,.3f} SUI")
        return out

    def strategy_check(self):
        """Бэктест всех стратегий из suibot.toml на ценах за 30 и 90 дней — в фоне, бот не останавливается.
        Сам стратегию не меняет: только присылает таблицу и подсказку."""
        self.last_strategy_check = time.time()
        cfg, cur = self.cfg, self.s.name
        strategies = [x for x in cfg.strategies if x.rebalance != "none"]

        def work():
            try:
                res = {}
                pools = {x.pool: cfg.pools[x.pool] for x in strategies}
                now = read_pools(pools)
                td = history.trend_days(strategies)
                for days in (30, 90):
                    cs, ys = history.load(pools, days, 5)
                    times, prices = [c[0] for c in cs], [c[1] for c in cs]
                    warm = history.trend_warmup(td, times[0]) if td else None
                    res[days] = (prices[-1] / prices[0] - 1, {
                        x.name: simulate(x, cfg.pools[x.pool], times, prices, ys[x.pool], cfg.costs,
                                         now[x.pool]["spacing"], scale=prices[0] / now[x.pool]["sui"], warm=warm)
                        for x in strategies})
                gain = {d: {n: r["value_sui"] / r["capital_sui"] - 1 for n, r in rows.items()}
                        for d, (_, rows) in res.items()}
                lines = ["🧪 <b>Проверка стратегий</b> — сколько стало штук SUI на истории"]
                for d, (move, _) in res.items():
                    lines += ["", f"<b>{d} дней</b> (цена SUI {pct(move)}):"]
                    for n, g in sorted(gain[d].items(), key=lambda kv: -kv[1]):
                        lines.append(("▸ <b>" if n == cur else "  ") + f"{esc(n)}: {pct(g)} SUI"
                                     + (" ← сейчас</b>" if n == cur else ""))
                better = [n for n in gain[30] if n != cur and all(gain[d][n] > gain[d].get(cur, 0) + 0.02 for d in gain)]
                if better:
                    best = max(better, key=lambda n: gain[30][n] + gain[90][n])
                    pool = next(x.pool for x in strategies if x.name == best)
                    move = (f"; она в другом пуле ({pool}): после смены бот продолжит в старом пуле, а переедет "
                            "после /close и /resume" if pool != self.s.pool else "")
                    lines += ["", f"💡 «{esc(best)}» лучше текущей в обоих периодах больше чем на 2%. Можно "
                              f"переключить: в suibot.toml [live] strategy = \"{esc(best)}\"{esc(move)} — решение за вами"]
                else:
                    lines += ["", "✅ Текущая стратегия не хуже остальных — менять не нужно"]
                lines.append(f"🧠 <i>почему: раз в strategy_check_days = {self.lc.strategy_check_days:g} дн. бот гоняет "
                             "все стратегии из suibot.toml на свежих 5-минутных ценах; прошлое не гарантирует "
                             "будущего, поэтому бот сам ничего не меняет</i>")
                notify.send("\n".join(lines), html=True)
            except Exception as e:  # noqa: BLE001 — проверка не должна мешать боту
                notify.send(f"🧪 Проверка стратегий не удалась: {str(e)[:200]}")

        threading.Thread(target=work, daemon=True).start()

    def snapshot(self, st):
        """Снимок раз в час в live/snapshots.csv — для недельного графика и разбора."""
        now = time.time()
        if now - self.last_snap_t < 3600 or not self.book or not self.book.start:
            return
        self.last_snap_t = now
        b = self.book
        r = summary(b, st, self.price_of(st))
        lp = b.mode == "lp" and b.L
        _append(self.dir / "snapshots.csv", {
            "t": round(now), "time_utc": _utc(now), "price": round(r["price"], 5),
            "lo": round(r["range"][0], 5) if lp else "", "hi": round(r["range"][1], 5) if lp else "",
            "value": round(r["value"], 2), "value_sui": round(r["value_sui"], 3), "capital_sui": round(r["capital_sui"], 3),
            "earned": round(self.earned(st, r), 4), "in_range": int(bool(lp and r["in_range_now"])), "mode": b.mode})

    def shadow_lines(self, st) -> list[str]:
        """«Тень»: бумажные копии стратегий (служба suibot-paper) против реального бота — сколько стало штук SUI."""
        path = self.cfg.state_dir / "state.json"
        if not path.exists() or not self.book or not self.book.start:
            return []
        try:
            paper = json.loads(path.read_text())
            books = {n: Book(**d) for n, d in paper.get("books", {}).items()}
        except (ValueError, TypeError):
            return []

        def pool_state(pool):
            """Состояние другого пула — по последней цене, которую видела «тень»."""
            if pool == self.book.pool:
                return st
            q = (paper.get("prev") or {}).get(pool)
            if not q or not q.get("sq"):
                return None
            pc = self.cfg.pools[pool]
            return dict(state_from_price(raw_to_usd(q["sq"] ** 2, pc.a_is_sui), pc.a_is_sui), t=q["t"])
        r = summary(self.book, st, self.price_of(st))
        lines = ["", "🌗 <b>Тень</b> — бумажные копии на тех же ценах (сколько стало штук SUI)",
                 f"▸ <b>реальный бот: {pct(r['value_sui'] / r['capital_sui'] - 1)}</b>"]
        since = None
        for x in self.cfg.strategies:
            bk = books.get(x.name)
            xs = pool_state(x.pool) if bk and bk.start and x.rebalance != "none" else None
            if not xs:
                continue
            q = summary(bk, xs, self.price_of(xs))
            since = since or bk.start["t"]
            late = (f" (с {datetime.fromtimestamp(bk.start['t']).strftime('%d.%m')})"
                    if bk.start["t"] - since > 86400 else "")             # добавлена в suibot.toml позже остальных
            lines.append(f"  {esc(x.name)}{' (как реальный)' if x.name == self.s.name else ''}: "
                         f"{pct(q['value_sui'] / q['capital_sui'] - 1)}{late}")
        if since:
            lines.append(f"<i>бумага с {datetime.fromtimestamp(since).strftime('%d.%m')}, реальный бот с "
                         f"{datetime.fromtimestamp(self.book.start['t']).strftime('%d.%m')}; разница реального и "
                         "бумажного — цена исполнения</i>")
        return lines if len(lines) > 3 else []

    def weekly(self, st):
        """Недельный отчёт: итоги за 7 дней, «тень», график и журнал событий файлом."""
        if not self.book or not self.book.start:
            notify.send("📅 Позиция ещё не открыта — недельного отчёта нет")
            return
        now = time.time()
        path = self.dir / "snapshots.csv"
        rows = [x for x in csv.DictReader(path.open()) if now - float(x["t"]) <= 7 * 86400] if path.exists() else []
        r = summary(self.book, st, self.price_of(st))
        lines = ["📅 <b>Неделя</b> · " + esc(self.s.name)]
        if rows:
            a = rows[0]
            got = self.earned(st, r) - float(a["earned"])
            vs0 = float(a["value_sui"]) - float(a["capital_sui"])
            lines += [f"с {datetime.fromtimestamp(float(a['t'])).strftime('%d.%m %H:%M')}",
                      f"цена SUI {pct(r['price'] / float(a['price']) - 1)}",
                      f"заработано <b>{money(got)}</b> ≈ {got / r['price']:,.2f} SUI",
                      f"к «держать SUI» за неделю {num(r['vs_hold_sui_count'] - vs0, 2)} SUI",
                      f"в диапазоне {sum(int(x['in_range']) for x in rows) / len(rows):.0%} времени"]
        else:
            lines.append("снимков пока нет — график появится через пару часов работы")
        ev = self.dir / "events.csv"
        if ev.exists():
            cut = datetime.fromtimestamp(now - 7 * 86400, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            acts = [x["event"] for x in csv.DictReader(ev.open()) if x["time_utc"] >= cut and x["mode"] == self.label]
            n = {k: acts.count(k) for k in ("пересборка", "выход в SUI", "выход в USDC", "возврат в пул",
                                             "реинвестирование", "ошибка") if acts.count(k)}
            lines.append("действия: " + (", ".join(f"{k} — {v}" for k, v in n.items()) or "не было"))
        lines += self.shadow_lines(st)
        lines.append("🧠 <i>почему: раз в неделю (понедельник утром) — итоги, график и журнал; журнал events.csv "
                     "перешлите мне — по нему улучшаем стратегию</i>")
        notify.send("\n".join(lines), html=True)
        try:
            png = charts.weekly_chart(rows, self.dir / "week.png")
            if png:
                notify.send_file(png, "цена и диапазон · штуки SUI у бота против «держать SUI»", photo=True)
        except Exception:  # noqa: BLE001 — график не главное
            log(traceback.format_exc())
        if ev.exists():
            notify.send_file(ev, "журнал событий бота (events.csv)")
        self.trend_report()
        self.calibration(st)

    def trend_report(self, wait: bool = False):
        """Тренд SUI на масштабах 50/100/200/365 дней по дневным ценам Binance — в фоне, отдельным сообщением."""
        days = self.s.trend_ma_days

        def work():
            try:
                notify.send("\n".join(scales_lines(history.daily_closes(900), days)), html=True)
            except Exception as e:  # noqa: BLE001 — картина рынка не должна мешать боту
                notify.send(f"🧭 Тренд по масштабам не удался: {str(e)[:200]}")

        if wait:
            work()
        else:
            threading.Thread(target=work, daemon=True).start()

    def manual_count(self, t0: float) -> int:
        """Ручные команды и паузы с момента t0 — модель их не делает."""
        ev = self.dir / "events.csv"
        if not ev.exists():
            return 0
        cut = _utc(t0)
        return sum(1 for x in csv.DictReader(ev.open()) if x["time_utc"] >= cut and x["mode"] == self.label
                   and (x["event"].startswith("вручную") or x["event"] == "пауза"))

    def calibration(self, st, wait: bool = False):
        """Сверка «факт против модели»: тот же симулятор, по которому выбиралась стратегия, прогоняется с запуска
        бота на реальных 5-минутных ценах и доходе пула и сравнивается с тем, что бот получил на деле. В фоне:
        история грузится минуту-две, бот не останавливается (wait — сразу, для проверок)."""
        b = self.book
        if not b or not b.start:
            notify.send("🔬 Позиция ещё не открыта — сверять пока нечего")
            return
        real = summary(b, st, self.price_of(st))
        earned = self.earned(st, real)
        t0, now = b.start["t"], st["t"]
        s = replace(self.s, capital_sui=b.start["capital_sui"])
        pool, pc, spacing, costs = b.pool, self.cfg.pools[b.pool], st["spacing"], self.cfg.costs
        manual = self.manual_count(t0)

        def work():
            try:
                cs, ys = history.load({pool: pc}, min(365, math.ceil((now - t0) / 86400) + 1), 5)
                i0 = next((i for i, c in enumerate(cs) if c[0] >= t0), len(cs))
                if len(cs) - i0 < 12:
                    notify.send("🔬 Бот работает меньше часа — сверять с моделью пока рано")
                    return
                warm = history.trend_warmup(s.trend_ma_days, cs[i0][0]) if s.trend_ma_days else None
                model = simulate(s, pc, [c[0] for c in cs[i0:]], [c[1] for c in cs[i0:]], ys[pool][i0:], costs, spacing,
                                 warm=warm)
                notify.send("\n".join(calibration_lines(real, earned, model, manual)), html=True)
            except Exception as e:  # noqa: BLE001 — сверка не должна мешать боту
                notify.send(f"🔬 Сверка с моделью не удалась: {str(e)[:200]}")

        if wait:
            work()
        else:
            threading.Thread(target=work, daemon=True).start()

    def ping(self):
        """Отметка «жив» для healthchecks.io раз в healthcheck_minutes (адрес — HEALTHCHECK_URL в .env или файл
        healthcheck_url.txt в папке состояния). Нет отметок — сервис сам пишет вам, что бот не работает."""
        now = time.time()
        if now - self.last_ping < self.lc.healthcheck_minutes * 60:
            return
        f = self.cfg.state_dir / "healthcheck_url.txt"
        url = os.environ.get("HEALTHCHECK_URL") or (f.read_text().strip() if f.exists() else "")
        if not url:
            return
        self.last_ping = now
        threading.Thread(target=lambda: requests.get(url, timeout=10), daemon=True).start()

    def downtime_notice(self, st):
        """После запуска: если бот не работал дольше downtime_notice_minutes — сколько и что было с ценой."""
        since, self.down_since = self.down_since, None
        gap = time.time() - since if since else 0
        if gap < self.lc.downtime_notice_minutes * 60:
            return
        text = f"не работал {age(gap / 86400)} (с {datetime.fromtimestamp(since).strftime('%d.%m %H:%M')})"
        try:
            cs = history.binance_candles(since, time.time(), 5 if gap < 3 * 86400 else 60)
            ps = [c[1] for c in cs]
            if ps:
                text += f"; цена SUI за это время ${min(ps):.4f}–${max(ps):.4f}"
                b = self.book
                if b and b.mode == "lp" and b.L:
                    lo, hi = b.range_usd
                    out = sum(1 for x in ps if not lo <= x <= hi) / len(ps)
                    text += f"; вне диапазона ≈{out:.0%} времени — тогда комиссии не шли"
        except Exception:  # noqa: BLE001 — история цены — не главное
            pass
        self.event(st["t"], "бот был выключен", st["sui"], text, why=self.why("бот был выключен"))

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
        if b.pool != self.s.pool and self.switch_pool(st):        # стратегия в другом пуле — переезд по команде
            return
        if not self.paused:
            if b.mode == "hold":                                     # после /sui, /usdc, /close и /resume
                self.act(st, "resume", "по команде")
            elif b.mode == "lp" and not self.pos_id and not self.dry:   # позиция не открылась или пропала
                d = self.skipped(st, decide(b, self.s, st, self.watch))
                if self.price_ok(st):
                    if d and d[0] in ("exit", "crash"):
                        self.act(st, *d)
                    else:
                        self.act(st, "reopen", "позиции нет")
            else:
                d = self.skipped(st, decide(b, self.s, st, self.watch))
                if d and self.price_ok(st):
                    self.act(st, *d)
            now = time.time()
            if not self.dry and self.lc.reinvest_days:
                if self.last_reinvest is None:
                    self.last_reinvest = now                  # первое реинвестирование — через reinvest_days
                elif now - self.last_reinvest >= self.lc.reinvest_days * 86400:
                    self.reinvest(st)
        self.errors, self.retry_at = 0, None

    def skipped(self, st, d):
        """Выход, пропущенный фильтром тренда: сообщить один раз (пока сигнал держится) и ничего не делать."""
        if d and d[0].startswith("skip"):
            kind = "выход в SUI пропущен" if d[0] == "skip_exit" else "выход в USDC пропущен"
            if self.skip_noted != kind:
                self.skip_noted = kind
                self.event(st["t"], kind, st["sui"], d[1] + "; бот остаётся в пуле", st, why=self.why(kind))
            return None
        if d is None:
            self.skip_noted = None
        return d

    def trend_warm(self):
        """Предыстория средней тренда с Binance (часовые цены): при запуске и, пока не получилось, раз в час."""
        if not self.s.trend_ma_days or self.watch.trend_ma() is not None or time.time() - self.trend_try < 3600:
            return
        self.trend_try = time.time()
        try:
            self.watch.warm_trend(history.trend_warmup(self.s.trend_ma_days, time.time()))
        except Exception as e:  # noqa: BLE001 — без предыстории фильтр ждёт, пока накопится своя история
            log(f"нет предыстории для средней тренда: {e}")

    def switch_pool(self, st) -> bool:
        """[live] strategy — в другом пуле, а деньги бота ещё в старом. Бот продолжает работать в старом пуле
        (по правилам новой стратегии), пока позиция не снята командой; после /close (/sui, /usdc) и /resume
        начинает заново в новом пуле с монет кошелька. True — книга сброшена, старт на следующем опросе."""
        b = self.book
        if b.mode == "hold" and not self.pos_id and not self.manual and not self.paused:
            old = b.pool
            self.book, self.pc, self.switch_noted = None, self.cfg.pools[self.s.pool], False
            self.watch.reset()
            self.event(st["t"], "переход в другой пул", st["sui"], f"{old} → {self.s.pool}; бот возьмёт свои монеты из "
                       "кошелька и откроет позицию в новом пуле", why=self.why("переход в другой пул"))
            self.save()
            return True
        if not self.switch_noted:
            self.switch_noted = True
            self.event(st["t"], "переход в другой пул", st["sui"], f"стратегия «{self.s.name}» работает в пуле "
                       f"{self.s.pool}, а позиция бота — в {b.pool}; пока бот продолжает в {b.pool}; чтобы переехать: "
                       "/close, затем /resume", why=self.why("переход в другой пул"))
        return False

    def failed(self, st, e: Exception):
        self.need_sync = not self.dry
        text = str(e) or type(e).__name__
        if isinstance(e, ReadError):                    # нет связи: повторять без паузы, сообщить один раз
            self.errors = min(self.errors + 1, len(RETRY_MINUTES))
            m = RETRY_MINUTES[self.errors - 1]
            self.retry_at = st["t"] + m * 60
            if self.errors == 1:
                self.event(st["t"], "нет связи", st["sui"], f"{text}; повтор через {m} мин", why=self.why("нет связи"))
            else:
                log(f"{text} — повтор через {m} мин")
            return
        self.errors += 1
        if not isinstance(e, (ExecError, subprocess.TimeoutExpired)):
            log(traceback.format_exc())
        if self.errors <= len(RETRY_MINUTES):
            m = RETRY_MINUTES[self.errors - 1]
            self.retry_at = st["t"] + m * 60
            self.event(st["t"], "ошибка", st["sui"], f"{text}; бот сверится с кошельком и повторит через {m} мин",
                       why=self.why("ошибка"))
        else:
            self.paused, self.retry_at = True, None
            self.event(st["t"], "ошибка", st["sui"], f"{text}; {self.errors}-я ошибка подряд — бот на паузе; "
                       "деньги в пуле или в кошельке: посмотрите Cetus и пришлите текст ошибки; /resume — продолжить",
                       why=self.why("ошибка"))

    def tick(self, snap: dict):
        st = snap[self.s.pool]
        try:
            if self.book and self.prev and st["t"] > self.prev["t"] and self.book.L:
                accrue_growth(self.book, self.prev, st, self.price_of(st))
            self.trend_warm()
            self.watch.add(st["t"], st["sui"])
            if self.down_since:
                self.downtime_notice(st)
            for c in self.read_commands():
                self.command(c, st)
            self.step(st)
            self.range_notice(st)
            self.check_alerts(st)
        except Exception as e:  # noqa: BLE001 — любая ошибка: сверка, повтор, затем пауза; бот не падает
            try:
                self.failed(st, e)
            except Exception:  # noqa: BLE001
                log(traceback.format_exc())
        finally:
            self.prev = {k: st[k] for k in ("t", "sq", "fa", "fb", "rew")}
            day = datetime.now(timezone.utc)
            now = time.time()
            text = None
            if self.book and self.book.start and self.hello:
                text = self.report_card(st, "Бот работает")                       # первый отчёт после запуска
            elif day.hour == self.cfg.report_hour_utc and self.last_report_day != day.strftime("%Y-%m-%d"):
                try:
                    text = self.report_card(st, "Утренний отчёт", daily=True)
                except Exception:  # noqa: BLE001
                    log(traceback.format_exc())
            elif (self.lc.report_hours and self.book and self.book.start and self.last_report_t
                  and now - self.last_report_t >= self.lc.report_hours * 3600):
                text = self.report_card(st, "Отчёт")
            self.hello = False
            if day.hour == self.cfg.report_hour_utc:
                self.last_report_day = day.strftime("%Y-%m-%d")
            if text or not self.last_report_t:
                self.last_report_t = now
            if text:
                try:
                    notify.send(text, html=True, buttons=self.buttons())
                except Exception:  # noqa: BLE001
                    log(traceback.format_exc())
            self.last_tick_t = now
            self.ping()
            try:
                self.snapshot(st)
                if text and "Утренний" in text and datetime.now().weekday() == 0:   # понедельник — недельный отчёт
                    self.weekly(st)
            except Exception:  # noqa: BLE001
                log(traceback.format_exc())
            if self.lc.strategy_check_days and self.book and self.book.start:
                if self.last_strategy_check is None:
                    self.last_strategy_check = now            # первая проверка — через strategy_check_days
                elif now - self.last_strategy_check >= self.lc.strategy_check_days * 86400:
                    self.strategy_check()
            try:
                self.save()
            except OSError as e:
                log(f"не удалось сохранить состояние: {e}")

    def run(self, ticks: int | None = None):
        if not self.dry and not self.has_key:
            raise SystemExit("для реальных денег нужен ключ: read -s SUI_PRIVATE_KEY && export SUI_PRIVATE_KEY "
                             "(или dry_run = true)")
        try:
            lock = lock_once(self.dir / "lock")
        except OSError:
            raise SystemExit("бот уже запущен в другом окне — второй экземпляр не нужен") from None
        ctl = self.dir / "control.txt"
        if ctl.exists():                               # команды, отданные, пока бот не работал, не выполняются
            log(f"пропущены старые команды: {' '.join(ctl.read_text().split())}")
            ctl.unlink()
        log(f"боевой режим [{self.label}]: «{self.s.name}», до ${self.lc.max_capital_usd:,.0f}, опрос каждые "
            f"{self.cfg.poll_seconds} с; команды — Telegram или python3 bot.py control <команда>")
        notify.set_menu(MENU)
        notify.send(f"🟢 <b>Бот запущен</b>" + (" · 🧪 симуляция" if self.dry else " · реальные деньги")
                    + f"\nстратегия «{esc(self.s.name)}», лимит ${self.lc.max_capital_usd:,.0f}"
                    + f"\nцена проверяется каждые {self.cfg.poll_seconds} с"
                    + (f", отчёт каждые {self.lc.report_hours:g} ч" if self.lc.report_hours else "")
                    + "\nкоманды — кнопка «/» в чате или /help", html=True)
        self.hello = True
        self.down_since = self.last_tick_t
        n, why = 0, None
        try:
            while ticks is None or n < ticks:
                try:
                    snap = read_pools({self.s.pool: self.pc}, fast=True)
                except Exception as e:  # noqa: BLE001 — сеть и лимиты не должны останавливать бота
                    log(f"не удалось прочитать пул: {e}")
                    self.net_down_since = self.net_down_since or time.time()
                else:
                    if self.net_down_since and time.time() - self.net_down_since >= 120:
                        st = snap[self.s.pool]
                        self.event(st["t"], "связь восстановлена", st["sui"], "не было связи с сетью Sui "
                                   f"{age((time.time() - self.net_down_since) / 86400)}", why=self.why("связь восстановлена"))
                    self.net_down_since = None
                    self.tick(snap)
                n += 1
                if ticks is None or n < ticks:
                    time.sleep(self.cfg.poll_seconds)
        except KeyboardInterrupt:
            why = "остановлен (Ctrl+C или перезапуск службы)"
            log("остановлен (позиция в пуле остаётся); при следующем запуске бот сверится с кошельком")
        except Exception as e:  # noqa: BLE001
            why = f"аварийно остановлен: {e}"
            raise
        finally:
            self.save()
            lock.close()
            if why:
                notify.send(f"🔴 <b>Бот {esc(why)}</b>" + (" · 🧪 симуляция" if self.dry else "")
                            + "\n⚠️ позиция осталась в пуле без присмотра: пересборок и выходов не будет, "
                            "пока бот не запущен снова", html=True)
        if self.book and self.book.start:
            try:
                print(self.report(read_pools({self.s.pool: self.pc})[self.s.pool]))
            except Exception:  # noqa: BLE001
                pass


def show(cfg: Config, events: int = 10):
    """Состояние боевого режима из сохранённых файлов и текущей цены пула — ничего не отправляет и не меняет
    (можно запускать, пока бот работает, например чтобы Claude Code проверил его)."""
    bot = Live(cfg)
    st = read_pools({bot.s.pool: bot.pc})[bot.s.pool]
    print(bot.report(st))
    notes = []
    if bot.paused:
        notes.append("бот на паузе")
    if bot.errors:
        notes.append(f"ошибок подряд: {bot.errors}")
    if bot.pending:
        notes.append(f"неподтверждённая транзакция: {bot.pending.get('op')}")
    if bot.manual:
        notes.append(f"незавершённая команда: /{bot.manual}")
    if bot.pos_id:
        notes.append(f"позиция {bot.pos_id}")
    if notes:
        print("; ".join(notes))
    path = bot.dir / "events.csv"
    if path.exists():
        lines = path.read_text().splitlines()
        print(f"\nпоследние события ({path}):")
        print("\n".join(lines[-events:] if len(lines) <= events else [lines[0]] + lines[-events:]))


def control(cfg: Config, cmd: str):
    """Команда работающему боту через файл (из другого окна терминала)."""
    if cmd.split(" ")[0] not in COMMANDS:
        raise SystemExit(HELP)
    d = cfg.state_dir / "live"
    d.mkdir(parents=True, exist_ok=True)
    with (d / "control.txt").open("a") as f:
        f.write(cmd + "\n")
    print(f"команда «{cmd}» передана боту — выполнится на следующем опросе")


def check(cfg: Config, env_file: str):
    """Проверка перед запуском (ключ и токены не печатаются): .env, Telegram, кошелёк, исполнитель, настройки."""
    bad = 0

    def line(good: bool, text: str):
        nonlocal bad
        bad += not good
        print(("✅ " if good else "❌ ") + text)

    present = [n for n in ("SUI_PRIVATE_KEY", "TG_TOKEN", "TG_CHAT") if os.environ.get(n)]
    line(len(present) == 3, f"{env_file}: найдены {', '.join(present) or 'ничего'}"
         + ("" if len(present) == 3 else " — нужны SUI_PRIVATE_KEY, TG_TOKEN, TG_CHAT"))
    key = os.environ.get("SUI_PRIVATE_KEY", "")
    line(key.startswith("suiprivkey1"), "ключ в формате suiprivkey1…" if key.startswith("suiprivkey1")
         else "ключ не в формате suiprivkey1… (экспортируйте приватный ключ из Slush заново)")
    err = notify.send("✅ проверка связи: бот видит этот чат")
    line(err is None, "Telegram: тестовое сообщение отправлено — проверьте чат" if err is None else f"Telegram: {err}")
    lc, s = cfg.live, next(x for x in cfg.strategies if x.name == cfg.live.strategy)
    pc = cfg.pools[s.pool]
    try:
        w = executor("status", "--pool", pc.object, simulate=not key, address=None if key else lc.address)
        st = read_pools({s.pool: pc})[s.pool]
        bal = {norm(k): int(v) for k, v in w["balances"].items()}
        sui, usdc = bal.get(norm(SUI), 0) / 1e9, bal.get(norm(USDC), 0) / 1e6
        usd = max(0.0, sui - lc.gas_reserve_sui) * st["sui"] + usdc
        line(True, f"кошелёк {w['address'][:10]}…{w['address'][-4:]}: {sui:,.2f} SUI + {usdc:,.2f} USDC "
                   f"(SUI ${st['sui']:.4f})")
        line(usd >= min(lc.max_capital_usd, 5) and sui >= lc.gas_reserve_sui + 0.5,
             f"бот возьмёт ${min(usd, lc.max_capital_usd):,.2f} из лимита ${lc.max_capital_usd:,.0f}"
             + (f"; до лимита не хватает ≈{(lc.max_capital_usd - usd) / st['sui'] + max(0.0, lc.gas_reserve_sui - sui):,.1f} SUI"
                if usd < lc.max_capital_usd else "")
             + f" (в кошельке всегда остаётся {lc.gas_reserve_sui:g} SUI на газ)")
        if w.get("positions"):
            print(f"ℹ️  в кошельке уже есть позиции в этом пуле ({len(w['positions'])}) — бот их не тронет")
    except (ExecError, subprocess.TimeoutExpired, KeyError, ValueError) as e:
        line(False, f"кошелёк/исполнитель: {e}")
    real = cfg.state_dir / "live" / "state_real.json"
    print(f"ℹ️  dry_run = {str(lc.dry_run).lower()} — "
          + ("пробный режим, ничего не отправляется" if lc.dry_run else "РЕАЛЬНЫЕ ДЕНЬГИ")
          + (f"; есть состояние прошлого боевого запуска ({real.name})" if real.exists() else ""))
    print("\nвсё готово" if not bad else f"\nпроблем: {bad} — исправьте и запустите проверку ещё раз")
