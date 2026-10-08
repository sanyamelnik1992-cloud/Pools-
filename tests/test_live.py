"""Боевой режим с поддельным исполнителем (без сети и ключей):  python3 tests/test_live.py  (или pytest).

Поддельный исполнитель ведёт кошелёк и позиции по той же математике пула, что и бот, и отвечает так же, как
настоящий (изменения балансов, созданная позиция, наличие позиции у кошелька). Проверяется весь цикл и сбои:
потерянный ответ сети после отправки, позиция, закрытая вручную, прерванный выход, ошибки подряд, чужие позиции
и лишние деньги в кошельке, разделение симуляции и реальных денег, странные команды из Telegram.
"""
import json
import math
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import suibot.live as live  # noqa: E402
from sui_pools import USDC  # noqa: E402
from suibot import notify  # noqa: E402
from suibot.chain import state_from_price  # noqa: E402
from suibot.clmm import amounts, sqrt_of_tick  # noqa: E402
from suibot.config import load  # noqa: E402
from suibot.rally import RallyWatch  # noqa: E402

SUI_LONG = "0x" + "0" * 63 + "2::sui::SUI"
CETUS = "0x6864a6f921804860930db6ddbe2e16acdf8504495ea7481637a1c8b9a8fe54b::cetus::CETUS"
ADDR = "0xb0t"
GAS = 0.01e9


class FakeChain:
    """Кошелёк и пул. lose — ответ на эту команду теряется после того, как транзакция прошла;
    fail — команда падает до отправки (симуляция не проходит)."""

    def __init__(self, sui: float, usdc: float):
        self.w = {"sui": sui * 1e9, "usdc": usdc * 1e6, "cetus": 0.0}
        self.pos = {}
        self.n = 0
        self.st = None
        self.calls = []
        self.lose = set()
        self.fail = set()
        self.abort = set()        # транзакция исполнилась с ошибкой: списан только газ
        self.hide = 0             # столько опросов новые позиции не видны в списке (отстающий узел)
        self.read_down = False    # чтение кошелька недоступно
        self.pools = []           # (команда, объект пула) реальных вызовов

    def changes(self, sui=0.0, usdc=0.0):
        return [{"coinType": SUI_LONG, "address": ADDR, "amount": str(int(sui))},
                {"coinType": USDC, "address": ADDR, "amount": str(int(usdc))}]

    def add_position(self, L, tl, th):
        self.n += 1
        pid = f"pos{self.n}"
        self.pos[pid] = (L, tl, th)
        return pid

    def value(self):
        st = self.st
        v = self.w["sui"] / 1e9 * st["sui"] + self.w["usdc"] / 1e6
        for L, tl, th in self.pos.values():
            a, b = amounts(L, st["sq"], sqrt_of_tick(tl), sqrt_of_tick(th))
            v += a / 1e6 + b / 1e9 * st["sui"]
        return v

    def __call__(self, *args, simulate, address=None):
        a = [str(x) for x in args]
        cmd, kv = a[0], dict(zip(a[1::2], a[2::2]))
        self.calls.append((cmd, simulate))
        if "--pool" in kv and not simulate:
            self.pools.append((cmd, kv["--pool"]))
        st = self.st
        if cmd == "status":
            if self.read_down:
                raise live.ExecError("fetch failed")
            listed = {k: v for k, v in self.pos.items() if not (self.hide and k == f"pos{self.n}")}
            self.hide = max(0, self.hide - 1)
            out = {"ok": True, "address": ADDR,
                   "positions": [{"id": k, "liquidity": str(L), "tick_lower": tl, "tick_upper": th}
                                 for k, (L, tl, th) in listed.items()],
                   "balances": {SUI_LONG: str(int(self.w["sui"])), USDC: str(int(self.w["usdc"])),
                                **({CETUS: str(int(self.w["cetus"]))} if self.w["cetus"] else {})}}
            if "--position" in kv:
                pid = kv["--position"]
                out["position_owned"] = pid in self.pos
                if pid in self.pos:
                    L, tl, th = self.pos[pid]
                    out["position"] = {"id": pid, "liquidity": str(L), "tick_lower": tl, "tick_upper": th}
            return out
        if cmd in self.fail:
            raise live.ExecError(f"{cmd}: симуляция не прошла")
        if cmd == "close" and kv["--position"] not in self.pos:
            raise live.ExecError("Object not found")
        if simulate:
            return {"ok": True, "simulated": True,
                    "wallet": {SUI_LONG: str(int(self.w["sui"])), USDC: str(int(self.w["usdc"]))}}
        if cmd in self.abort:
            self.w["sui"] -= GAS
            raise live.ExecError("MoveAbort", {"ok": False, "sent": True, "digest": "d", "status": {"success": False},
                                               "balance_changes": self.changes(-GAS, 0)})
        if cmd == "swap":
            amt = float(kv["--amount"])
            if kv["--from"] == CETUS:                                           # награды → SUI по $0.03 за CETUS
                assert amt <= self.w["cetus"] + 1, "бот меняет больше CETUS, чем есть"
                out = amt / 1e9 * 0.03 / st["sui"] * 1e9
                self.w["cetus"] -= amt
                self.w["sui"] += out - GAS
                res = {"ok": True, "balance_changes": self.changes(out - GAS, 0) +
                       [{"coinType": CETUS, "address": ADDR, "amount": str(int(-amt))}]}
            elif kv["--from"] == USDC:
                assert amt <= self.w["usdc"] + 1, "бот меняет больше USDC, чем есть"
                out = amt / 1e6 / st["sui"] * 0.999 * 1e9
                self.w["usdc"] -= amt
                self.w["sui"] += out - GAS
                res = {"ok": True, "balance_changes": self.changes(out - GAS, -amt)}
            else:
                assert amt + GAS <= self.w["sui"] + 1, "бот меняет больше SUI, чем есть"
                out = amt / 1e9 * st["sui"] * 0.999 * 1e6
                self.w["sui"] -= amt + GAS
                self.w["usdc"] += out
                res = {"ok": True, "balance_changes": self.changes(-amt - GAS, out)}
        elif cmd == "open":
            tl, th = int(kv["--tick-lower"]), int(kv["--tick-upper"])
            have_a, have_b = float(kv["--amount-a"]), float(kv["--amount-b"])   # a — USDC, b — SUI
            assert have_a <= self.w["usdc"] + 1 and have_b + GAS <= self.w["sui"] + 1, "не хватает монет в кошельке"
            a1, b1 = amounts(1.0, st["sq"], sqrt_of_tick(tl), sqrt_of_tick(th))
            L = min(have_a / a1 if a1 else math.inf, have_b / b1 if b1 else math.inf) * 0.999
            ua, ub = L * a1, L * b1
            self.w["usdc"] -= ua
            self.w["sui"] -= ub + GAS
            pid = self.add_position(L, tl, th)
            res = {"ok": True, "balance_changes": self.changes(-ub - GAS, -ua), "created_positions": [pid],
                   "position": {"id": pid, "liquidity": str(L), "tick_lower": tl, "tick_upper": th}}
        elif cmd == "collect":                                                  # комиссии и 100 CETUS наград
            fa, fb, rw = 0.5e6, 0.4e9, 100e9
            self.w["usdc"] += fa
            self.w["sui"] += fb - GAS
            self.w["cetus"] += rw
            res = {"ok": True, "balance_changes": self.changes(fb - GAS, fa) +
                   [{"coinType": CETUS, "address": ADDR, "amount": str(int(rw))}]}
        elif cmd == "add":
            pid = kv["--position"]
            L0, tl, th = self.pos[pid]
            have_a, have_b = float(kv["--amount-a"]), float(kv["--amount-b"])
            assert have_a <= self.w["usdc"] + 1 and have_b + GAS <= self.w["sui"] + 1, "не хватает монет в кошельке"
            a1, b1 = amounts(1.0, st["sq"], sqrt_of_tick(tl), sqrt_of_tick(th))
            L = min(have_a / a1 if a1 else math.inf, have_b / b1 if b1 else math.inf) * 0.999
            self.w["usdc"] -= L * a1
            self.w["sui"] -= L * b1 + GAS
            self.pos[pid] = (L0 + L, tl, th)
            res = {"ok": True, "balance_changes": self.changes(-L * b1 - GAS, -L * a1),
                   "position": {"id": pid, "liquidity": str(L0 + L), "tick_lower": tl, "tick_upper": th}}
        elif cmd == "close":
            L, tl, th = self.pos.pop(kv["--position"])
            a, b = amounts(L, st["sq"], sqrt_of_tick(tl), sqrt_of_tick(th))
            a, b = a * 1.001, b * 1.001                                         # немного комиссий
            self.w["usdc"] += a
            self.w["sui"] += b - GAS
            res = {"ok": True, "balance_changes": self.changes(b - GAS, a)}
        else:
            raise AssertionError(cmd)
        if cmd in self.lose:
            self.lose.discard(cmd)
            raise live.ExecError("ответ сети потерян")                         # транзакция прошла, ответа нет
        return res


def config(tmp: Path, dry=False, extra="", strategy="тест"):
    toml = (Path(__file__).resolve().parent.parent / "suibot.toml").read_text()
    head = toml.split("[[strategy]]")[0].replace('state_dir = "data/private/bot"', f'state_dir = "{tmp}"')
    head = re.sub(r"(?m)^dry_run = (true|false)", f"dry_run = {'true' if dry else 'false'}", head)  # любой режим в suibot.toml
    head = "\n".join(f'strategy = "{strategy}"' if x.startswith("strategy =") else x for x in head.splitlines())
    head += '''
[[strategy]]
name = "тест"
pool = "cetus_005"
capital_sui = 100
range_down = 0.04
range_up = 0.04
out_minutes = 180
rally_exit = [[72, 0.15]]
resume_drop_pct = 0.10
crash_exit = [[72, 0.15]]
resume_rise_pct = 0.10
''' + extra
    p = tmp / "t.toml"
    p.write_text(head)
    return load(p)


def snap(price, t):
    st = dict(state_from_price(price, False), t=t, spacing=10, fa=0, fb=0, rew={})
    return {"cetus_005": st, "cetus_025": st}


def bot_value(b, st):
    a, bb = b.holdings(st)
    return a * st["ua"] + bb * st["ub"]


class Harness:
    def __init__(self, d, chain, dry=False):
        self.d, self.chain, self.t = Path(d), chain, 0.0
        live.executor = chain
        self.sent = []
        notify.send = lambda text, html=False, buttons=None: self.sent.append(text)
        notify.send_file = lambda path, caption='', photo=False: self.sent.append(f'FILE {Path(path).name} {photo}')
        notify.commands = lambda offset: ([], offset)
        live.exchange_price = lambda: None                                        # без Binance: сверка не мешает
        live.history.load = lambda *a, **k: ([], {})                              # без сети: история пустая
        live.history.trend_warmup = lambda days, end: []                          # без сети: предыстории нет
        live.history.daily_closes = lambda days: []                               # без сети: дневных цен нет
        self.bot = live.Live(config(self.d, dry))

    def tick(self, price, dt=3600):
        self.t += dt
        s = snap(price, self.t)
        self.chain.st = s["cetus_005"]
        self.bot.tick(s)
        return s["cetus_005"]

    def cmd(self, c):
        (self.d / "live" / "control.txt").write_text(c + "\n")

    def restart(self):
        self.bot = live.Live(config(self.d, self.bot.dry))


def test_live_cycle():
    with tempfile.TemporaryDirectory() as d:
        chain = FakeChain(sui=500, usdc=0)
        h = Harness(d, chain)
        st = h.tick(1.20)
        bot, b = h.bot, h.bot.book
        assert bot.pos_id == "pos1" and b.L > 0 and not bot.paused
        assert abs(bot_value(b, st) - 200) < 3                             # лимит капитала $200
        assert chain.w["sui"] / 1e9 > 300                                  # остальное в кошельке не тронуто
        assert ("swap", True) in chain.calls and ("swap", False) in chain.calls   # симуляция перед отправкой
        h.tick(1.28, dt=600)                                               # вышли из ±4%, но меньше 180 мин
        assert bot.pos_id == "pos1"
        st = h.tick(1.28, dt=3 * 3600)                                     # 3 часа вне диапазона: пересборка
        assert len(b.rebalances) == 1 and b.range_usd[0] < 1.28 < b.range_usd[1] and bot.pos_id == "pos2"
        st = h.tick(1.42)                                                  # +18% от минимума за 72 ч: выход в SUI
        assert b.mode == "sui" and bot.pos_id is None and b.idle_a < 1e3 and len(b.exits) == 1
        assert chain.w["usdc"] < 1e3 and not chain.pos                     # весь USDC бота обменян на SUI
        h.cmd("pause")
        h.tick(1.42)
        h.cmd("resume")                                                    # /resume в режиме SUI не возвращает в пул
        h.tick(1.42)
        assert b.mode == "sui" and not chain.pos and not bot.paused
        st = h.tick(1.25)                                                  # −12% от пика: возврат в пул
        assert b.mode == "lp" and bot.pos_id and len(b.resumes) == 1
        h.cmd("usdc")
        st = h.tick(1.25)                                                  # вручную: всё в USDC и пауза
        assert bot.paused and bot.pos_id is None and b.idle_b < 0.1e9 and b.mode == "hold" and not chain.pos
        h.tick(1.10)                                                       # на паузе бот ничего не делает
        assert bot.pos_id is None
        h.cmd("resume")
        st = h.tick(1.10)
        assert not bot.paused and bot.pos_id and b.mode == "lp" and len(chain.pos) == 1
        assert b.idle_a >= -1 and b.idle_b >= -0.02e9                      # свои монеты бота не уходят в минус
        assert chain.w["sui"] / 1e9 > 299                                  # чужие SUI кошелька целы
        assert 150 < bot_value(b, st) < 260 and b.costs_usd > 0
        h.restart()                                                        # перезапуск: состояние на месте
        assert h.bot.pos_id == bot.pos_id and h.bot.book.L == b.L


def test_crash_exit_and_return():
    with tempfile.TemporaryDirectory() as d:
        chain = FakeChain(sui=170, usdc=0)
        h = Harness(d, chain)
        h.tick(1.20)
        st = h.tick(1.00)                                                  # −17% за 72 ч: выход в USDC
        b = h.bot.book
        assert b.mode == "usdc" and not chain.pos and (b.idle_b - live.GAS_BUFFER_SUI * 1e9) * st["ub"] < 1.5
        h.tick(0.95)
        assert b.mode == "usdc"
        h.tick(1.06)                                                       # +12% от минимума: возврат в пул
        assert b.mode == "lp" and len(chain.pos) == 1


def test_lost_response_on_open():
    """Открытие прошло, но ответ потерян: бот находит свою позицию в кошельке и не открывает вторую."""
    with tempfile.TemporaryDirectory() as d:
        chain = FakeChain(sui=500, usdc=0)
        h = Harness(d, chain)
        h.tick(1.20)
        h.tick(1.28)
        chain.lose.add("open")
        st = h.tick(1.28, dt=4 * 3600)                                     # пересборка: закрыли, открыли, ответа нет
        bot = h.bot
        assert bot.errors == 1 and bot.retry_at and not bot.paused
        st = h.tick(1.28)                                                  # повтор: сверка нашла позицию
        assert len(chain.pos) == 1 and bot.pos_id in chain.pos and bot.errors == 0
        assert bot_value(bot.book, st) < 215                               # не больше, чем было у бота
        assert any("позиция найдена" in x.lower() for x in h.sent)


def test_lost_response_on_close_and_ctrl_c():
    """Закрытие прошло, ответ потерян, бот перезапущен: позиция не числится, монеты учтены, позиция открыта заново."""
    with tempfile.TemporaryDirectory() as d:
        chain = FakeChain(sui=170, usdc=0)
        h = Harness(d, chain)
        h.tick(1.20)
        st = h.tick(1.28)
        before = bot_value(h.bot.book, st)
        chain.lose.add("close")
        h.tick(1.28, dt=4 * 3600)
        h.restart()                                                        # как Ctrl+C и новый запуск
        st = h.tick(1.28)
        bot = h.bot
        assert not bot.paused and len(chain.pos) == 1 and bot.pos_id in chain.pos
        assert abs(bot_value(bot.book, st) - before) < 2                   # монеты из закрытой позиции учтены
        assert bot_value(bot.book, st) < chain.value() - 1.0 * st["sui"]   # резерв на газ не тронут


def test_position_closed_by_hand():
    with tempfile.TemporaryDirectory() as d:
        chain = FakeChain(sui=170, usdc=0)
        h = Harness(d, chain)
        h.tick(1.20)
        L, tl, th = chain.pos.pop("pos1")                                  # пользователь закрыл позицию в Cetus
        a, b = amounts(L, chain.st["sq"], sqrt_of_tick(tl), sqrt_of_tick(th))
        chain.w["usdc"] += a
        chain.w["sui"] += b
        h.tick(1.20)
        bot = h.bot
        assert not bot.paused and bot.pos_id == "pos1"                     # один ответ узла — ещё не повод
        h.tick(1.20)
        assert bot.paused and bot.pos_id is None and not chain.pos         # подтвердилось: бот не открывает сам — пауза
        h.cmd("resume")
        h.tick(1.20)
        assert not bot.paused and len(chain.pos) == 1


def test_interrupted_exit_is_completed():
    """Сильный рост: позиция снята, а обмен в SUI не прошёл — бот доводит выход до конца, а не возвращается в пул."""
    with tempfile.TemporaryDirectory() as d:
        chain = FakeChain(sui=170, usdc=0)
        h = Harness(d, chain)
        h.tick(1.20)
        chain.fail.add("swap")
        h.tick(1.42)
        b = h.bot.book
        assert b.mode == "sui" and not chain.pos and h.bot.errors == 1
        chain.fail.clear()
        h.tick(1.42)
        assert b.mode == "sui" and not chain.pos and chain.w["usdc"] < 1e6 and h.bot.errors == 0


def test_errors_retry_then_pause():
    with tempfile.TemporaryDirectory() as d:
        chain = FakeChain(sui=170, usdc=0)
        h = Harness(d, chain)
        h.tick(1.20)
        h.tick(1.30)
        chain.fail.update({"open", "close", "swap"})
        for _ in range(4):
            h.tick(1.30, dt=4 * 3600)
        assert h.bot.paused and h.bot.errors == 4 and len(chain.pos) == 1  # позиция на месте, бот на паузе
        assert any("на паузе" in x for x in h.sent)
        chain.fail.clear()
        h.cmd("resume")
        h.tick(1.30)
        assert not h.bot.paused and h.bot.book.rebalances


def test_start_failure_and_resume():
    with tempfile.TemporaryDirectory() as d:
        chain = FakeChain(sui=2, usdc=0)                                   # в кошельке только резерв на газ
        h = Harness(d, chain)
        for _ in range(5):
            h.tick(1.20)
        h.cmd("status")
        h.tick(1.20)                                                       # команды работают и без позиции
        assert h.bot.paused and h.bot.book is None and not chain.pos
        chain.w["sui"] += 100e9
        h.cmd("resume")
        h.tick(1.20)
        assert not h.bot.paused and len(chain.pos) == 1


def test_foreign_positions_and_extra_money():
    """В кошельке чужая позиция и много чужих денег: бот их не трогает даже после потерянного ответа."""
    with tempfile.TemporaryDirectory() as d:
        chain = FakeChain(sui=3000, usdc=1000)
        foreign = chain.add_position(1e9, 66000, 68000)
        h = Harness(d, chain)
        h.tick(1.20)
        h.tick(1.30)
        chain.lose.add("close")
        h.tick(1.30, dt=4 * 3600)
        st = h.tick(1.30)
        bot = h.bot
        assert foreign in chain.pos and bot.pos_id != foreign and len(chain.pos) == 2
        assert bot_value(bot.book, st) < 225                               # не взял чужие деньги после сбоя


def test_dry_and_real_states_are_separate():
    with tempfile.TemporaryDirectory() as d:
        chain = FakeChain(sui=170, usdc=0)
        h = Harness(d, chain, dry=True)
        h.tick(1.20)
        h.tick(1.42)                                                       # виртуальный выход в SUI
        assert h.bot.book.mode == "sui" and not chain.pos                  # в симуляции ничего не отправлено
        h2 = Harness(d, chain, dry=False)
        h2.t = h.t
        h2.tick(1.42)
        assert h2.bot.book.mode == "lp" and len(chain.pos) == 1            # реальный режим начался с нуля
        assert json.loads((Path(d) / "live" / "state_dry.json").read_text())["book"]["mode"] == "sui"


def test_telegram_commands_are_robust():
    import requests
    updates = [{"update_id": 1, "message": {"chat": {"id": 42}, "from": {"id": 42}, "text": "/"}},
               {"update_id": 2, "message": {"chat": {"id": 42}, "from": {"id": 42}, "text": "/@mybot"}},
               {"update_id": 3, "message": {"chat": {"id": 42}, "from": {"id": 7}, "text": "/usdc"}},
               {"update_id": 4, "message": {"chat": {"id": 9}, "from": {"id": 9}, "text": "/close"}},
               {"update_id": 5, "edited_message": {"text": "/sui"}},
               {"update_id": 6, "message": {"chat": {"id": 42}, "from": {"id": 42}, "text": "/Status@mybot now"}}]

    class R:
        def json(self):
            return {"ok": True, "result": updates}

    import importlib
    import os
    n = importlib.reload(notify)
    old = requests.get, os.environ.get("TG_TOKEN"), os.environ.get("TG_CHAT")
    requests.get = lambda *a, **k: R()
    os.environ.update(TG_TOKEN="x", TG_CHAT="42")
    try:
        cmds, offset = n.commands(None)
    finally:
        requests.get = old[0]
        for k, v in (("TG_TOKEN", old[1]), ("TG_CHAT", old[2])):
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    assert cmds == ["status"] and offset == 7


def test_watch_rules_changed():
    w = RallyWatch([[72, 0.15]], {"rise": [[[0, 1.0]]], "drop": []}, drop_rules=[[72, 0.15]])
    assert len(w.dq) == 1 and not w.dq[0] and w.q[0]                   # добавили правило — наблюдение с нуля
    w = RallyWatch([[72, 0.15], [24, 0.1]], {"rise": [[[0, 1.0]]]})
    assert len(w.q) == 2 and not w.q[0]


def test_pause_after_errors_is_quiet():
    """После паузы из-за ошибок бот больше ничего не отправляет и не шлёт сообщений, пока не будет /resume."""
    with tempfile.TemporaryDirectory() as d:
        chain = FakeChain(sui=170, usdc=0)
        h = Harness(d, chain)
        h.tick(1.20)
        chain.abort.add("swap")                                            # обмен проходит симуляцию, но падает в сети
        h.cmd("usdc")
        for _ in range(5):
            h.tick(1.20, dt=1800)
        assert h.bot.paused and h.bot.errors == 4
        calls, sent = len(chain.calls), len(h.sent)
        for _ in range(10):
            h.tick(1.20, dt=30)
        assert len(chain.calls) == calls and len(h.sent) == sent


def test_read_errors_do_not_pause():
    with tempfile.TemporaryDirectory() as d:
        chain = FakeChain(sui=170, usdc=0)
        h = Harness(d, chain)
        h.tick(1.20)
        chain.read_down = True
        n = len(h.sent)
        for _ in range(10):
            h.tick(1.20, dt=1800)
        assert not h.bot.paused and len(h.sent) == n + 1                   # одно сообщение «нет связи», без паузы
        chain.read_down = False
        h.tick(1.20, dt=1800)
        assert h.bot.errors == 0 and len(chain.pos) == 1


def test_resume_after_failed_manual_keeps_pause():
    """/usdc сорвался, пользователь нажал /resume: бот доводит /usdc до конца и остаётся на паузе, а не идёт в пул."""
    with tempfile.TemporaryDirectory() as d:
        chain = FakeChain(sui=170, usdc=0)
        h = Harness(d, chain)
        h.tick(1.20)
        chain.fail.add("swap")
        h.cmd("usdc")
        h.tick(1.20)
        chain.fail.clear()
        h.cmd("resume")
        h.tick(1.20)
        b = h.bot.book
        assert h.bot.paused and b.mode == "hold" and not chain.pos and b.idle_b < 0.1e9


def test_lost_response_on_resume_does_not_exit_again():
    with tempfile.TemporaryDirectory() as d:
        chain = FakeChain(sui=170, usdc=0)
        h = Harness(d, chain)
        h.tick(1.20)
        h.tick(1.42)                                                       # выход в SUI
        chain.lose.add("open")
        h.tick(1.25)                                                       # возврат в пул, ответ потерян
        h.tick(1.25)
        b = h.bot.book
        assert b.mode == "lp" and len(b.exits) == 1 and len(b.resumes) == 1 and len(chain.pos) == 1


def test_failed_open_on_resume_no_swap_pingpong():
    with tempfile.TemporaryDirectory() as d:
        chain = FakeChain(sui=170, usdc=0)
        h = Harness(d, chain)
        h.tick(1.20)
        h.tick(1.42)
        swaps = sum(1 for c, sim in chain.calls if c == "swap" and not sim)
        chain.fail.add("open")
        for _ in range(3):
            h.tick(1.25)
        assert sum(1 for c, sim in chain.calls if c == "swap" and not sim) - swaps <= 1   # один обмен к доле, не туда-обратно
        chain.fail.clear()
        h.tick(1.25)
        b = h.bot.book
        assert b.mode == "lp" and len(chain.pos) == 1 and len(b.exits) == 1


def test_lost_swap_does_not_take_user_coins():
    """Ответ на обмен при выходе в SUI потерян, а в кошельке $1000 USDC пользователя: бот их не трогает."""
    with tempfile.TemporaryDirectory() as d:
        chain = FakeChain(sui=170, usdc=1000)
        h = Harness(d, chain)
        h.tick(1.20)
        usdc_before = chain.w["usdc"]
        chain.lose.add("swap")
        h.tick(1.42)
        st = h.tick(1.42)
        b = h.bot.book
        assert b.mode == "sui" and b.idle_a < 1e6                          # у бота почти нет USDC
        assert chain.w["usdc"] > usdc_before - 1e6 and abs(bot_value(b, st) - 200) < 10   # USDC пользователя на месте


def test_position_hidden_by_lagging_node():
    """Открытие прошло, ответ потерян, а список позиций узла отстаёт: бот ждёт, а не открывает вторую."""
    with tempfile.TemporaryDirectory() as d:
        chain = FakeChain(sui=500, usdc=0)
        h = Harness(d, chain)
        h.tick(1.20)
        h.tick(1.28)
        chain.lose.add("open")
        chain.hide = 2
        for _ in range(4):
            h.tick(1.28, dt=4 * 3600)
        assert len(chain.pos) == 1 and h.bot.pos_id in chain.pos and not h.bot.foreign


def test_unsent_close_keeps_fee_estimate_once():
    with tempfile.TemporaryDirectory() as d:
        chain = FakeChain(sui=500, usdc=0)
        h = Harness(d, chain)
        h.tick(1.20)
        b = h.bot.book
        b.fees_a += 5e6                                                    # оценка комиссий $5
        st = h.tick(1.20)
        before = bot_value(b, st)
        h.bot.pending = {"op": "close", "id": h.bot.pos_id}                # отправка не случилась
        h.bot.need_sync = True
        st = h.tick(1.20)
        assert bot_value(b, st) <= before + 0.5


def test_range_notices_and_report():
    """Цена вышла из диапазона — одно сообщение; вернулась — одно сообщение; /status — позиция и заработок."""
    with tempfile.TemporaryDirectory() as d:
        chain = FakeChain(sui=170, usdc=0)
        h = Harness(d, chain)
        h.tick(1.20)
        n = len(h.sent)
        h.tick(1.27, dt=120)                                                # вне диапазона, но меньше 5 минут
        assert len(h.sent) == n
        for _ in range(3):
            h.tick(1.27, dt=300)
        out = [x for x in h.sent[n:] if "вне диапазона" in x]
        assert len(out) == 1 and "пересборка примерно через" in out[0]
        h.tick(1.20, dt=300)
        assert sum("снова в диапазоне" in x for x in h.sent[n:]) == 1
        h.cmd("status")
        h.tick(1.20, dt=60)
        rep = h.sent[-1]
        assert "до нижней" in rep and "Заработок" in rep and "Капитал" in rep and "pos1" in rep and "●" in rep
        assert h.bot.pos_id == "pos1"                                      # пересборки не было


def test_why_alerts_events_gas_daily():
    """Пересборка объясняет «почему», алерт срабатывает один раз, /events и сводка за сутки, мало газа."""
    with tempfile.TemporaryDirectory() as d:
        chain = FakeChain(sui=170, usdc=0)
        h = Harness(d, chain)
        st = h.tick(1.20)
        assert any("почему:" in x and "max_capital_usd" in x for x in h.sent)        # «открыта» с объяснением
        h.cmd("alert 1,26")
        h.tick(1.20, dt=60)
        assert h.bot.alerts == [[1.26, "up"]]
        for _ in range(4):
            h.tick(1.27, dt=3600)                                               # вне диапазона > 3 ч — пересборка
        reb = [x for x in h.sent if x.startswith("🔄")]
        assert reb and "почему:" in reb[0] and "out_minutes" in reb[0]
        assert sum("сработал ваш алерт" in x for x in h.sent) == 1 and not h.bot.alerts
        h.cmd("events")
        h.tick(1.27, dt=60)
        assert "Последние события" in h.sent[-1] and "пересборка" in h.sent[-1]
        card = h.bot.report_card(h.chain.st, "Утренний отчёт", daily=True)
        assert "С запуска" in card and h.bot.day_snap
        assert "За сутки" in h.bot.report_card(h.chain.st, "Утренний отчёт", daily=True)
        h.bot.lc.gas_warn_sui = 1000                                              # «мало газа» при любой сумме
        h.bot.need_sync = True
        h.tick(1.27, dt=60)
        h.bot.need_sync = True
        h.tick(1.27, dt=60)
        assert sum("газ" in x and "пополните" in x for x in h.sent) == 1          # одно предупреждение, без повторов


def test_telegram_buttons_and_arguments():
    import importlib
    import os
    import requests
    updates = [{"update_id": 10, "callback_query": {"id": "c1", "from": {"id": 42}, "data": "pause",
                                                     "message": {"chat": {"id": 42}}}},
               {"update_id": 11, "callback_query": {"id": "c2", "from": {"id": 7}, "data": "close",
                                                     "message": {"chat": {"id": 7}}}},
               {"update_id": 12, "message": {"chat": {"id": 42}, "from": {"id": 42}, "text": "/alert 1.30 лишнее"}}]

    class R:
        ok = True

        def json(self):
            return {"ok": True, "result": updates}

    n = importlib.reload(notify)
    old = requests.get, requests.post, os.environ.get("TG_TOKEN"), os.environ.get("TG_CHAT")
    answered = []
    requests.get = lambda *a, **k: R()
    requests.post = lambda *a, **k: answered.append(k.get("json")) or R()
    os.environ.update(TG_TOKEN="x", TG_CHAT="42")
    try:
        cmds, offset = n.commands(None)
    finally:
        requests.get, requests.post = old[0], old[1]
        for k, v in (("TG_TOKEN", old[2]), ("TG_CHAT", old[3])):
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    assert cmds == ["pause", "alert 1.30"] and offset == 13                   # чужая кнопка не принята
    assert answered == [{"callback_query_id": "c1"}]


def test_reinvest_and_price_check():
    """Реинвестирование: комиссии забраны, CETUS → SUI, всё добавлено в ту же позицию. Сверка с биржей откладывает
    пересборку, пока цены расходятся."""
    import time as _t
    with tempfile.TemporaryDirectory() as d:
        chain = FakeChain(sui=170, usdc=0)
        h = Harness(d, chain)
        old_price = live.token_price
        live.token_price = lambda t, sui: (0.03, 9) if t.endswith("CETUS") else (sui, 9) if t.endswith("SUI") else (1.0, 6)
        try:
            h.tick(1.20)
            L0 = chain.pos["pos1"][0]
            h.bot.last_reinvest = _t.time() - 8 * 86400
            h.tick(1.20, dt=60)
            msg = [x for x in h.sent if x.startswith("♻️")]
            assert msg and "добавлено в позицию" in msg[0] and "CETUS" in msg[0] and "почему:" in msg[0]
            assert chain.pos["pos1"][0] > L0 and h.bot.pos_id == "pos1"            # та же позиция, ликвидности больше
            assert chain.w["cetus"] < 1 and h.bot.collected.get(CETUS, 0) < 1      # награды обменяны
            assert not h.bot.pending and h.bot.errors == 0
            assert abs(h.bot.last_reinvest - _t.time()) < 60                          # следующий раз — через неделю
        finally:
            live.token_price = old_price
        live.exchange_price = lambda: 1.40                                         # биржа далеко от пула
        for _ in range(4):
            h.tick(1.27, dt=3600)
        assert h.bot.pos_id == "pos1" and sum("расходится" in x for x in h.sent) == 1   # ждёт, сообщил один раз
        live.exchange_price = lambda: 1.27
        h.tick(1.27, dt=60)
        assert h.bot.pos_id != "pos1"                                              # цены сошлись — пересборка


def test_weekly_report_with_shadow_and_chart():
    """Снимки раз в час, недельный отчёт: итоги, «тень» (бумажные копии), график и журнал файлом."""
    from dataclasses import asdict
    with tempfile.TemporaryDirectory() as d:
        chain = FakeChain(sui=170, usdc=0)
        h = Harness(d, chain)
        for p in (1.20, 1.21, 1.19, 1.22):
            h.bot.last_snap_t = 0                                               # снимок на каждом такте
            h.tick(p, dt=3600)
        rows = (Path(d) / "live" / "snapshots.csv").read_text().splitlines()
        assert len(rows) == 5 and rows[0].startswith("t,time_utc,price")
        (Path(d) / "state.json").write_text(json.dumps({"books": {"тест": asdict(h.bot.book)}}))   # бумажная копия
        h.cmd("week")
        h.tick(1.22, dt=60)
        week = [x for x in h.sent if x.startswith("📅")]
        assert week and "заработано" in week[0] and "Тень" in week[0] and "реальный бот" in week[0]
        files = [x for x in h.sent if x.startswith("FILE")]
        assert "FILE events.csv False" in files
        try:
            import matplotlib  # noqa: F401
            assert "FILE week.png True" in files
        except ImportError:
            pass


POOL_025 = '''
[[strategy]]
name = "тест 0.25"
pool = "cetus_025"
capital_sui = 100
range_down = 0.04
range_up = 0.04
out_minutes = 180
rally_exit = [[72, 0.15]]
resume_drop_pct = 0.10
crash_exit = [[72, 0.15]]
resume_rise_pct = 0.10
'''


def test_calibration_lines():
    real = {"days": 10, "value_sui": 105, "capital_sui": 100, "rebalances": 4, "exits": 0, "crashes": 0,
            "costs_usd": 1.0, "in_range_pct": 80.0}
    model = dict(real, value_sui=106, rebalances=5, fees_usd=10.0, in_range_pct=85.0)
    text = "\n".join(live.calibration_lines(real, 9.0, model, manual=2))
    assert "90%" in text and "подтверждается" in text and "ручных команд" in text and "в диапазоне" in text
    assert "ниже модели" in "\n".join(live.calibration_lines(real, 5.0, model))
    assert "выше модели" in "\n".join(live.calibration_lines(real, 15.0, model))
    assert "данных пока мало" in "\n".join(live.calibration_lines(dict(real, days=1), 9.0, model))


def test_model_vs_fact():
    """/model: тот же симулятор с запуска бота на ценах и доходе пула этих дней — сравнение с реальным ботом."""
    import threading
    with tempfile.TemporaryDirectory() as d:
        chain = FakeChain(sui=170, usdc=0)
        h = Harness(d, chain)
        prices = [1.20, 1.21, 1.19, 1.22, 1.20, 1.18]
        for p in prices:
            h.tick(p)
        t0 = h.bot.book.start["t"]

        def fake_load(pools, days, minutes=5, source="binance"):
            assert days >= 1
            cs = [(t0 + i * 300, prices[min(i // 12, len(prices) - 1)], 1.0) for i in range(12 * len(prices))]
            return cs, {k: [2e-5] * len(cs) for k in pools}
        old = live.history.load
        live.history.load = fake_load
        try:
            h.cmd("model")
            h.tick(1.20, dt=60)
            for t in threading.enumerate():
                if t is not threading.current_thread():
                    t.join(10)
        finally:
            live.history.load = old
        rep = [x for x in h.sent if x.startswith("🔬 <b>Факт против модели")]
        assert rep and "заработано" in rep[0] and "от модели" in rep[0] and "штук SUI" in rep[0], h.sent[-3:]


def test_shadow_includes_other_pool():
    from dataclasses import asdict
    with tempfile.TemporaryDirectory() as d:
        chain = FakeChain(sui=170, usdc=0)
        h = Harness(d, chain)
        h.bot = live.Live(config(Path(d), extra=POOL_025))
        h.tick(1.20)
        b = h.bot.book
        paper = {"books": {"тест": asdict(b), "тест 0.25": asdict(b)},
                 "prev": {"cetus_025": {"t": h.t, "sq": h.chain.st["sq"], "fa": 0, "fb": 0, "rew": {}}}}
        (Path(d) / "state.json").write_text(json.dumps(paper))
        text = "\n".join(h.bot.shadow_lines(h.chain.st))
        assert "тест 0.25" in text and "тест (как реальный)" in text


def test_pool_switch_only_after_close():
    """Стратегию переключили на другой пул: бот продолжает в старом пуле, переезжает только после /close и /resume."""
    with tempfile.TemporaryDirectory() as d:
        chain = FakeChain(sui=170, usdc=0)
        h = Harness(d, chain)
        cfg = config(Path(d), extra=POOL_025)
        h.bot = live.Live(cfg)
        h.tick(1.20)
        old_obj, new_obj = cfg.pools["cetus_005"].object, cfg.pools["cetus_025"].object
        assert ("open", old_obj) in chain.pools
        h.bot = live.Live(config(Path(d), extra=POOL_025, strategy="тест 0.25"))   # смена стратегии и перезапуск
        assert h.bot.pc.object == old_obj
        h.tick(1.20)
        h.tick(1.28)
        h.tick(1.28, dt=4 * 3600)                                          # пересборка — всё ещё в старом пуле
        assert ("close", old_obj) in chain.pools and ("open", new_obj) not in chain.pools
        assert sum("переход в другой пул" in x.lower() for x in h.sent) == 1   # предупреждение один раз
        h.cmd("close")
        h.tick(1.28)
        assert not chain.pos and h.bot.paused
        h.cmd("resume")
        h.tick(1.28)                                                       # книга сброшена
        h.tick(1.28)                                                       # старт в новом пуле
        assert ("open", new_obj) in chain.pools and len(chain.pos) == 1 and h.bot.book.pool == "cetus_025"
        assert h.bot.book.start["capital_sui"] * 1.28 < 205                # не больше лимита капитала


TREND = """
[[strategy]]
name = "тест тренд"
pool = "cetus_005"
capital_sui = 100
range_down = 0.04
range_up = 0.04
out_minutes = 180
rally_exit = [[72, 0.15]]
resume_drop_pct = 0.10
crash_exit = [[72, 0.15]]
resume_rise_pct = 0.10
trend_ma_days = 50
"""


def test_trend_filter_live():
    """Средняя за 50 дней из предыстории Binance: рост ниже средней — выход в SUI пропущен (одно сообщение),
    рост выше средней — выход как обычно; в отчёте — строка тренда."""
    for level, exits in ((2.0, 0), (0.5, 1)):
        with tempfile.TemporaryDirectory() as d:
            chain = FakeChain(sui=170, usdc=0)
            h = Harness(d, chain)
            live.history.trend_warmup = lambda days, end, lv=level: [(end - (60 * 24 - i) * 3600, lv)
                                                                    for i in range(60 * 24)]
            h.bot = live.Live(config(Path(d), extra=TREND, strategy="тест тренд"))
            h.t = 1.79e9                                                    # время как в сети (для предыстории)
            st = h.tick(1.20)
            assert h.bot.watch.trend_ma() is not None
            for _ in range(3):
                h.tick(1.42)                                                 # +18% за 72 ч
            b = h.bot.book
            assert len(b.exits) == exits and (b.mode == "lp") == (exits == 0)
            skipped = [x for x in h.sent if "Выход в SUI пропущен" in x]
            assert len(skipped) == (1 - exits) and (not skipped or "почему" in skipped[0])
            card = h.bot.report_card(h.chain.st)
            assert "тренд 50 дн." in card
    live.history.trend_warmup = lambda days, end: []


def test_switch_to_trend_strategy_keeps_position():
    """Переключение работающего бота на стратегию с фильтром тренда (тот же пул): позиция остаётся, средняя
    подгружается из предыстории, лишних транзакций нет."""
    with tempfile.TemporaryDirectory() as d:
        chain = FakeChain(sui=170, usdc=0)
        h = Harness(d, chain)
        h.t = 1.79e9
        h.bot = live.Live(config(Path(d), extra=TREND))
        h.tick(1.20)
        txs = lambda: [c for c in chain.pools if c[0] != "status"]           # noqa: E731 — только транзакции
        pos, calls = h.bot.pos_id, len(txs())
        live.history.trend_warmup = lambda days, end: [(end - (60 * 24 - i) * 3600, 1.0) for i in range(60 * 24)]
        h.bot = live.Live(config(Path(d), extra=TREND, strategy="тест тренд"))
        h.tick(1.20)
        assert h.bot.pos_id == pos and len(txs()) == calls and h.bot.watch.trend_ma() is not None
        assert h.bot.book.start and not h.bot.paused
    live.history.trend_warmup = lambda days, end: []


def test_trend_scales():
    """Картина рынка: падение 500 дней с максимума, дно, рост 60 дней — короткие масштабы растут, годовой ещё нет."""
    import threading
    day = 86400.0
    closes = [(i * day, 5.0 * (0.13 ** (i / 500))) for i in range(501)]                     # 5.0 → 0.65
    closes += [((501 + i) * day, 0.65 * (1.7 ** (i / 60))) for i in range(61)]               # дно → +70%
    lines = live.scales_lines(closes, 50)
    text = "\n".join(lines)
    assert "от максимума $5.00" in text and "от дна $0.650" in text
    assert "🟢 <b>50 дн.</b>" in text and "← фильтр бота" in text and "🔴 <b>365 дн.</b>" in text
    assert "🟡 <b>200 дн.</b>" in text                     # цена выше, но средняя ещё падает — смешанно
    assert "годовой разворот подтвердится" in text and "растущий тренд на 2 из 4" in text
    assert "нет дневных цен" in live.scales_lines([])[0]
    with tempfile.TemporaryDirectory() as d:
        h = Harness(d, FakeChain(sui=170, usdc=0))
        live.history.daily_closes = lambda days: closes
        try:
            h.cmd("trend")
            h.tick(1.20)
            for t in threading.enumerate():
                if t is not threading.current_thread():
                    t.join(10)
        finally:
            live.history.daily_closes = lambda days: []
        assert any(x.startswith("🧭 <b>Тренд SUI по масштабам") for x in h.sent)


if __name__ == "__main__":
    tests = [(n, f) for n, f in globals().items() if n.startswith("test_")]
    for n, f in tests:
        f()
        print("ok", n)
    print(f"все проверки пройдены: {len(tests)}")
