"""Боевой режим с поддельным исполнителем (без сети и ключей):  python3 tests/test_live.py  (или pytest).

Поддельный исполнитель ведёт кошелёк и позицию по той же математике пула, что и бот, и отвечает
изменениями балансов, как настоящий. Проверяется весь цикл: старт с лимитом капитала, пересборка,
выход в SUI на росте, возврат после отката, ручные команды и учёт своих денег бота.
"""
import math
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import suibot.live as live  # noqa: E402
from sui_pools import USDC  # noqa: E402
from suibot.chain import state_from_price  # noqa: E402
from suibot.clmm import amounts, sqrt_of_tick  # noqa: E402
from suibot.config import load  # noqa: E402

SUI_LONG = "0x" + "0" * 63 + "2::sui::SUI"
GAS = 0.01e9


class FakeChain:
    def __init__(self, sui: float, usdc: float):
        self.w = {"sui": sui * 1e9, "usdc": usdc * 1e6}
        self.pos = {}
        self.opened = 0
        self.st = None
        self.calls = []

    def changes(self, sui=0.0, usdc=0.0):
        return [{"coinType": SUI_LONG, "amount": str(int(sui))}, {"coinType": USDC, "amount": str(int(usdc))}]

    def __call__(self, *args, simulate, address=None):
        a = [str(x) for x in args]
        cmd, kv = a[0], dict(zip(a[1::2], a[2::2]))
        self.calls.append((cmd, simulate))
        st = self.st
        if cmd == "status":
            return {"ok": True, "address": "0xtest", "positions": [],
                    "balances": {SUI_LONG: str(int(self.w["sui"])), USDC: str(int(self.w["usdc"]))}}
        if simulate:
            return {"ok": True, "simulated": True}
        if cmd == "swap":
            amt = float(kv["--amount"])
            if kv["--from"] == USDC:
                assert amt <= self.w["usdc"] + 1, "бот меняет больше USDC, чем есть"
                out = amt / 1e6 / st["sui"] * 0.999 * 1e9
                self.w["usdc"] -= amt
                self.w["sui"] += out - GAS
                ch = self.changes(out - GAS, -amt)
            else:
                assert amt + GAS <= self.w["sui"] + 1, "бот меняет больше SUI, чем есть"
                out = amt / 1e9 * st["sui"] * 0.999 * 1e6
                self.w["sui"] -= amt + GAS
                self.w["usdc"] += out
                ch = self.changes(-amt - GAS, out)
            return {"ok": True, "balance_changes": ch}
        if cmd == "open":
            tl, th = int(kv["--tick-lower"]), int(kv["--tick-upper"])
            have_a, have_b = float(kv["--amount-a"]), float(kv["--amount-b"])   # a — USDC, b — SUI
            assert have_a <= self.w["usdc"] + 1 and have_b <= self.w["sui"] + 1, "не хватает монет в кошельке"
            a1, b1 = amounts(1.0, st["sq"], sqrt_of_tick(tl), sqrt_of_tick(th))
            L = min(have_a / a1 if a1 else math.inf, have_b / b1 if b1 else math.inf)
            ua, ub = L * a1, L * b1
            self.w["usdc"] -= ua
            self.w["sui"] -= ub + GAS
            self.opened += 1
            pid = f"pos{self.opened}"
            self.pos[pid] = (L, tl, th)
            return {"ok": True, "balance_changes": self.changes(-ub - GAS, -ua),
                    "position": {"id": pid, "liquidity": str(L)}, "amount_a": str(int(ua)), "amount_b": str(int(ub))}
        if cmd == "close":
            L, tl, th = self.pos.pop(kv["--position"])
            a, b = amounts(L, st["sq"], sqrt_of_tick(tl), sqrt_of_tick(th))
            a, b = a * 1.001, b * 1.001                                         # немного комиссий
            self.w["usdc"] += a
            self.w["sui"] += b - GAS
            return {"ok": True, "balance_changes": self.changes(b - GAS, a)}
        raise AssertionError(cmd)


def config(tmp: Path):
    toml = (Path(__file__).resolve().parent.parent / "suibot.toml").read_text()
    head = toml.split("[[strategy]]")[0].replace('state_dir = "data/private/bot"', f'state_dir = "{tmp}"')
    head = head.replace("dry_run = true", "dry_run = false").replace('strategy = "накопление, возврат −10%"',
                                                                      'strategy = "тест"')
    head += '''
[[strategy]]
name = "тест"
pool = "cetus_005"
capital_sui = 100
range_down = 0.05
range_up = 0.05
rally_exit = [[72, 0.15]]
resume_drop_pct = 0.10
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


def test_live_cycle():
    with tempfile.TemporaryDirectory() as d:
        chain = FakeChain(sui=500, usdc=0)
        live.executor = chain
        bot = live.Live(config(Path(d)))
        t, p = 0.0, 1.20

        def tick(price, dt=3600):
            nonlocal t
            t += dt
            s = snap(price, t)
            chain.st = s["cetus_005"]
            bot.tick(s)
            return s["cetus_005"]

        st = tick(p)
        b = bot.book
        assert bot.pos_id == "pos1" and b.L > 0 and not bot.paused
        assert abs(bot_value(b, st) - 200) < 3                             # лимит капитала $200
        assert chain.w["sui"] / 1e9 > 300                                  # остальное в кошельке не тронуто
        assert ("swap", True) in chain.calls and ("swap", False) in chain.calls   # симуляция перед отправкой
        st = tick(1.30)                                                    # вышли вверх из ±5%: пересборка
        assert len(b.rebalances) == 1 and b.range_usd[0] < 1.30 < b.range_usd[1] and bot.pos_id == "pos2"
        st = tick(1.42)                                                    # +18% от минимума за 72 ч: выход в SUI
        assert b.mode == "sui" and bot.pos_id is None and b.idle_a < 1e3 and len(b.exits) == 1
        assert chain.w["usdc"] < 1e3                                       # весь USDC бота обменян на SUI
        st = tick(1.25)                                                    # −12% от пика: возврат в пул
        assert b.mode == "lp" and bot.pos_id and len(b.resumes) == 1
        (Path(d) / "live" / "control.txt").write_text("usdc\n")
        st = tick(1.25)                                                    # вручную: всё в USDC и пауза
        assert bot.paused and bot.pos_id is None and b.idle_b < 1e6 and b.mode == "hold"
        tick(1.10)                                                         # на паузе бот ничего не делает
        assert bot.pos_id is None
        (Path(d) / "live" / "control.txt").write_text("resume\n")
        st = tick(1.10)
        assert not bot.paused and bot.pos_id and b.mode == "lp"
        assert b.idle_a >= -1 and b.idle_b >= -0.02e9                      # свои монеты бота не уходят в минус
        assert chain.w["sui"] / 1e9 > 299                                  # чужие SUI кошелька целы
        assert 150 < bot_value(b, st) < 260


def test_error_pauses():
    with tempfile.TemporaryDirectory() as d:
        def broken(*args, simulate, address=None):
            if args[0] == "status":
                return {"ok": True, "address": "0xtest", "positions": [], "balances": {SUI_LONG: str(int(500e9))}}
            raise live.ExecError("сеть недоступна")
        live.executor = broken
        bot = live.Live(config(Path(d)))
        bot.tick(snap(1.2, 3600))
        assert bot.paused                                                  # ошибка — пауза, а не повторы


if __name__ == "__main__":
    tests = [(n, f) for n, f in globals().items() if n.startswith("test_")]
    for n, f in tests:
        f()
        print("ok", n)
    print(f"все проверки пройдены: {len(tests)}")
