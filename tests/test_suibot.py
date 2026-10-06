"""Проверки математики и учёта бота без сети:  python3 tests/test_suibot.py  (или pytest)."""
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from suibot.book import Costs, accrue_growth, init_book, rebalance, summary, update_out  # noqa: E402
from suibot.chain import PoolCfg, raw_to_usd, state_from_price, usd_to_raw  # noqa: E402
from suibot.clmm import Q64, U128, growth_delta, snap_ticks, sqrt_of_tick  # noqa: E402
from suibot.sim import simulate  # noqa: E402
from suibot.rally import RallyWatch  # noqa: E402
from suibot.strategy import Strategy  # noqa: E402

NOCOST = Costs(0.0, 0.0, 0.0)


def st(price, a_is_sui=False, t=0.0, spacing=10, fa=0, fb=0, rew=None):
    return dict(state_from_price(price, a_is_sui), t=t, spacing=spacing, fa=fa, fb=fb, rew=rew or {})


def no_price(_t):
    return 0.0, 9


def test_price_conversions():
    for a_is_sui in (False, True):
        for p in (0.5, 1.2, 3.0):
            assert math.isclose(raw_to_usd(usd_to_raw(p, a_is_sui), a_is_sui), p)


def test_snap_nearest_tick():
    lo, hi = snap_ticks(800.0, 900.0, 60)
    step = 1.0001 ** 30                                   # полшага сетки
    assert 800 / step <= sqrt_of_tick(lo) ** 2 <= 800 * step and 900 / step <= sqrt_of_tick(hi) ** 2 <= 900 * step
    assert lo % 60 == 0 and hi % 60 == 0
    a, b = snap_ticks(1000.0, 1000.1, 60)                 # уже одного шага — расширяется до шага
    assert b - a == 60


def test_growth_wraps():
    assert growth_delta(5, U128 - 5) == 10


def test_open_conserves_value_and_split():
    for a_is_sui in (False, True):
        s = Strategy("t", "p", capital_sui=1000, range_down=0.1, range_up=0.1)
        p0 = st(1.2, a_is_sui)
        b = init_book(s, PoolCfg("p", "0x0", a_is_sui=a_is_sui), p0, NOCOST)
        r = summary(b, p0, no_price)
        assert math.isclose(r["value"], 1200, rel_tol=1e-9)
        assert abs(r["sui_share"] - 50) < 3                  # симметричный диапазон — почти 50/50
        assert abs(b.range_usd[0] / 1.08 - 1) < 0.002 and abs(b.range_usd[1] / 1.32 - 1) < 0.002


def test_out_of_range_is_one_token():
    s = Strategy("t", "p", 1000, 0.05, 0.05)
    b = init_book(s, PoolCfg("p", "0x0"), st(1.2), NOCOST)
    assert summary(b, st(1.5), no_price)["sui_share"] < 1e-6   # выше диапазона — всё в USDC
    assert summary(b, st(1.0), no_price)["sui_share"] > 99.99  # ниже — всё в SUI


def test_accrual_only_in_range():
    s = Strategy("t", "p", 1000, 0.05, 0.05)
    g = 10 ** 18
    b = init_book(s, PoolCfg("p", "0x0"), st(1.2), NOCOST)
    assert accrue_growth(b, st(1.2, t=0), st(1.2, t=30, fa=g, fb=g), no_price) > 0
    assert math.isclose(b.fees_a, g * b.L / Q64) and b.in_range_s == 30
    b = init_book(s, PoolCfg("p", "0x0"), st(1.2), NOCOST)
    assert accrue_growth(b, st(1.5, t=0), st(1.5, t=30, fa=g, fb=g), no_price) == 0 and b.in_range_s == 0


def test_rebalance_rules_and_value():
    s = Strategy("t", "p", 1000, 0.05, 0.05, rebalance="down", out_minutes=10)
    b = init_book(s, PoolCfg("p", "0x0"), st(1.2), NOCOST)
    update_out(b, st(1.5, t=60))
    assert s.rebalance_reason(b, 1.5, 60 + 3600) is None       # выше диапазона: «down» не пересобирает
    b.out_since = None
    update_out(b, st(1.0, t=100))
    assert s.rebalance_reason(b, 1.0, 100) is None             # ещё не прошло 10 минут
    assert s.rebalance_reason(b, 1.0, 700) == "цена ниже диапазона"
    before = summary(b, st(1.0, t=700), no_price)["value"]
    rebalance(b, st(1.0, t=700), s, NOCOST)
    assert b.range_usd[0] < 1.0 < b.range_usd[1]
    assert math.isclose(summary(b, st(1.0, t=700), no_price)["value"], before, rel_tol=1e-9)


def test_costs_are_charged():
    s = Strategy("t", "p", 1000, 0.05, 0.05)
    costs = Costs(0.0005, 0.0005, 0.02)
    b = init_book(s, PoolCfg("p", "0x0"), st(1.2), costs)
    # из 1000 SUI половина меняется на USDC: 600 × 0.1% + газ 0.02 SUI
    assert math.isclose(b.costs_usd, 600 * 0.001 + 0.02 * 1.2, rel_tol=0.05)


def test_sui_count_and_simulation():
    s = Strategy("t", "p", 1000, 0.05, 0.05)
    pc = PoolCfg("p", "0x0")
    r = simulate(s, pc, [0.0, 300.0, 600.0], [1.2, 1.2, 1.2], [0.0, 0.0, 0.0], NOCOST, 10)
    assert math.isclose(r["value_sui"], 1000) and math.isclose(r["vs_hold_sui_count"], 0, abs_tol=1e-9)
    # цена ушла вверх за диапазон: позиция в USDC, SUI-эквивалент меньше стартового; с доходом — больше, чем без него
    passive = Strategy("t", "p", 1000, 0.05, 0.05, rebalance="none")
    up = simulate(passive, pc, [0.0, 300.0], [1.2, 1.5], [0.0, 0.0], NOCOST, 10)
    assert up["value_sui"] < 1000 and up["sui_share"] < 1e-6
    fee = simulate(s, pc, [0.0, 300.0, 600.0], [1.2, 1.2, 1.2], [0.0, 1e-4, 1e-4], NOCOST, 10)
    assert fee["value_sui"] > 1000 and fee["fees_usd"] > 0


def test_rally_watch():
    w = RallyWatch([[24, 0.10]])
    for h, p in enumerate([1.0, 0.95, 1.0, 1.04]):
        w.add(h * 3600, p)
    assert w.triggered(1.04) is None                       # от минимума 0.95 рост меньше 10%
    w.add(4 * 3600, 1.06)
    assert w.triggered(1.06) is not None                   # +11.6% от минимума за сутки
    w.add(40 * 3600, 1.06)
    assert w.triggered(1.06) is None                       # старый минимум вышел из окна


def test_rally_exit_and_resume():
    s = Strategy("t", "p", 1000, 0.05, 0.05, rally_exit=[[24, 0.10]], resume_drop_pct=0.10)
    plain = Strategy("t", "p", 1000, 0.05, 0.05)
    pc = PoolCfg("p", "0x0")
    times = [h * 3600.0 for h in range(8)]
    up = [1.0, 1.03, 1.06, 1.09, 1.12, 1.20, 1.30, 1.40]
    r = simulate(s, pc, times, up, [0.0] * 8, NOCOST, 10)
    r0 = simulate(plain, pc, times, up, [0.0] * 8, NOCOST, 10)
    assert r["mode"] == "sui" and r["exits"] == 1 and math.isclose(r["sui_share"], 100)
    assert r["value_sui"] > r0["value_sui"]                 # после выхода рост не продаёт SUI
    back = simulate(s, pc, times + [8 * 3600.0], up + [1.2], [0.0] * 9, NOCOST, 10)
    assert back["mode"] == "lp" and back["resumes"] == 1     # откат на 14% от пика — снова в пуле
    stay = simulate(Strategy("t", "p", 1000, 0.05, 0.05, rally_exit=[[24, 0.10]]), pc, times + [8 * 3600.0],
                    up + [1.2], [0.0] * 9, NOCOST, 10)
    assert stay["mode"] == "sui" and stay["resumes"] == 0    # без resume_drop_pct бот остаётся в SUI


def test_crash_exit_and_resume():
    s = Strategy("t", "p", 1000, 0.05, 0.05, crash_exit=[[24, 0.10]], resume_rise_pct=0.10)
    pc = PoolCfg("p", "0x0")
    times = [h * 3600.0 for h in range(8)]
    down = [1.0, 0.97, 0.94, 0.91, 0.88, 0.80, 0.70, 0.60]
    r = simulate(s, pc, times, down, [0.0] * 8, NOCOST, 10)
    plain = simulate(Strategy("t", "p", 1000, 0.05, 0.05), pc, times, down, [0.0] * 8, NOCOST, 10)
    assert r["mode"] == "usdc" and r["crashes"] == 1 and r["sui_share"] < 1e-6
    assert r["value"] > plain["value"]                      # после выхода падение не съедает капитал
    back = simulate(s, pc, times + [8 * 3600.0], down + [0.67], [0.0] * 9, NOCOST, 10)
    assert back["mode"] == "lp" and back["resumes"] == 1     # +12% от минимума — снова в пуле


def test_watch_drop_and_old_state():
    w = RallyWatch([[24, 0.10]], drop_rules=[[24, 0.10]])
    for h, p in enumerate([1.0, 1.05, 1.0]):
        w.add(h * 3600, p)
    assert w.dropped(0.95) is None and w.triggered(1.0) is None
    w.add(3 * 3600, 0.94)
    assert w.dropped(0.94) is not None                      # −10.5% от максимума 1.05
    old = RallyWatch([[24, 0.10]], state=[[[0.0, 1.0]]])     # состояние старого формата (список очередей роста)
    assert old.triggered(1.2) is not None and old.dump()["drop"] == []


if __name__ == "__main__":
    tests = [(n, f) for n, f in globals().items() if n.startswith("test_")]
    for n, f in tests:
        f()
        print("ok", n)
    print(f"все проверки пройдены: {len(tests)}")
