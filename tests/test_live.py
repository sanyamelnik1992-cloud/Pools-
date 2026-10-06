"""Боевой режим с поддельным исполнителем (без сети и ключей):  python3 tests/test_live.py  (или pytest).

Поддельный исполнитель ведёт кошелёк и позиции по той же математике пула, что и бот, и отвечает так же, как
настоящий (изменения балансов, созданная позиция, наличие позиции у кошелька). Проверяется весь цикл и сбои:
потерянный ответ сети после отправки, позиция, закрытая вручную, прерванный выход, ошибки подряд, чужие позиции
и лишние деньги в кошельке, разделение симуляции и реальных денег, странные команды из Telegram.
"""
import json
import math
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
ADDR = "0xb0t"
GAS = 0.01e9


class FakeChain:
    """Кошелёк и пул. lose — ответ на эту команду теряется после того, как транзакция прошла;
    fail — команда падает до отправки (симуляция не проходит)."""

    def __init__(self, sui: float, usdc: float):
        self.w = {"sui": sui * 1e9, "usdc": usdc * 1e6}
        self.pos = {}
        self.n = 0
        self.st = None
        self.calls = []
        self.lose = set()
        self.fail = set()

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
        st = self.st
        if cmd == "status":
            out = {"ok": True, "address": ADDR,
                   "positions": [{"id": k, "liquidity": str(L), "tick_lower": tl, "tick_upper": th}
                                 for k, (L, tl, th) in self.pos.items()],
                   "balances": {SUI_LONG: str(int(self.w["sui"])), USDC: str(int(self.w["usdc"]))}}
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
            return {"ok": True, "simulated": True}
        if cmd == "swap":
            amt = float(kv["--amount"])
            if kv["--from"] == USDC:
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


def config(tmp: Path, dry=False):
    toml = (Path(__file__).resolve().parent.parent / "suibot.toml").read_text()
    head = toml.split("[[strategy]]")[0].replace('state_dir = "data/private/bot"', f'state_dir = "{tmp}"')
    head = head.replace("dry_run = true", f"dry_run = {'true' if dry else 'false'}")
    head = "\n".join('strategy = "тест"' if x.startswith("strategy =") else x for x in head.splitlines())
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
'''
    p = tmp / "t.toml"
    p.write_text(head)
    return load(p)


def snap(price, t):
    st = dict(state_from_price(price, False), t=t, spacing=10, fa=0, fb=0, rew={})
    return {"cetus_005": st}


def bot_value(b, st):
    a, bb = b.holdings(st)
    return a * st["ua"] + bb * st["ub"]


class Harness:
    def __init__(self, d, chain, dry=False):
        self.d, self.chain, self.t = Path(d), chain, 0.0
        live.executor = chain
        self.sent = []
        notify.send = self.sent.append
        notify.commands = lambda offset: ([], offset)
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
        assert any("позиция найдена" in x for x in h.sent)


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
        assert bot.paused and bot.pos_id is None and not chain.pos         # бот не открывает сам — пауза
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


if __name__ == "__main__":
    tests = [(n, f) for n, f in globals().items() if n.startswith("test_")]
    for n, f in tests:
        f()
        print("ok", n)
    print(f"все проверки пройдены: {len(tests)}")
