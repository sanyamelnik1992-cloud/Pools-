"""Проверки математики и учёта бота без сети:  python3 tests/test_suibot.py  (или pytest)."""
import math
import sys
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from suibot.book import (Costs, accrue_growth, carry, close_perp, decide, enter_up, init_book, rebalance, step,  # noqa: E402
                         summary, update_out)
from suibot.chain import PoolCfg, raw_to_usd, state_from_price, usd_to_raw  # noqa: E402
from suibot import env  # noqa: E402
from suibot.clmm import Q64, U128, growth_delta, snap_ticks, sqrt_of_tick  # noqa: E402
from suibot.sim import simulate  # noqa: E402
from suibot.rally import Phase, RallyWatch, watch_for  # noqa: E402
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


def test_env_file():
    import os
    import tempfile
    text = ('# секреты\nexport TG_TOKEN="123:abc"\nCHAT_ID=42  # мой чат\nPRIVATE_KEY=\'suiprivkey1xyz\'\n'
            'мусор\nEMPTY=\n')
    assert env.parse(text) == {"TG_TOKEN": "123:abc", "TG_CHAT": "42", "SUI_PRIVATE_KEY": "suiprivkey1xyz", "EMPTY": ""}
    names = ("TG_TOKEN", "TG_CHAT", "SUI_PRIVATE_KEY", "EMPTY")
    saved = {n: os.environ.pop(n, None) for n in names}
    try:
        os.environ["TG_CHAT"] = "7"                          # заданное в окружении не перезаписывается
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / ".env"
            f.write_text(text)
            f.chmod(0o644)
            assert sorted(env.load(f)) == ["SUI_PRIVATE_KEY", "TG_TOKEN"]
            assert os.environ["TG_CHAT"] == "7" and os.environ["SUI_PRIVATE_KEY"] == "suiprivkey1xyz"
            assert os.name != "posix" or f.stat().st_mode & 0o077 == 0     # права сужены до владельца
            assert env.load(Path(d) / "нет такого") == []
    finally:
        for n, v in saved.items():
            os.environ.pop(n, None)
            if v is not None:
                os.environ[n] = v


def test_trend_watch():
    w = RallyWatch(None, trend_days=2)
    for h in range(30):                                     # 30 часов — меньше 80% окна в 2 дня
        w.add(h * 3600.0, 1.0)
    assert w.trend_ma() is None and w.trend(0.5) is None    # средней ещё нет — фильтр не действует
    for h in range(30, 60):
        w.add(h * 3600.0, 2.0)
    ma = w.trend_ma()
    assert ma is not None and 1.0 < ma < 2.0 and w.trend(1.9) == "up" and w.trend(1.0) == "down"
    w.add(59 * 3600.0 + 60, 9.0)                            # чаще раза в час в среднюю не попадает
    assert w.trend_ma() == ma
    w.reset()                                               # сброс после возврата в пул не трогает тренд
    assert w.trend_ma() == ma
    again = RallyWatch(None, w.dump(), trend_days=2)        # переживает перезапуск
    assert math.isclose(again.trend_ma(), ma)
    cold = RallyWatch(None, trend_days=2)
    cold.add(100 * 3600.0, 3.0)
    cold.warm_trend([(h * 3600.0, 1.0) for h in range(52, 100)])   # предыстория старше своих цен
    assert cold.trend_ma() is not None and cold.tq[-1] == (100 * 3600.0, 3.0)
    assert RallyWatch([[24, 0.1]]).dump().get("trend") is None     # без фильтра состояние как раньше


def test_trend_filter_exits():
    """Выход в SUI — только выше средней (иначе это отскок на падающем рынке), в USDC — только ниже."""
    pc = PoolCfg("p", "0x0")
    times = [h * 3600.0 for h in range(8)]
    high = [(-(60 * 24 - h) * 3600.0, 2.0) for h in range(60 * 24)]     # 60 дней по $2: средняя выше цены
    low = [(-(60 * 24 - h) * 3600.0, 0.5) for h in range(60 * 24)]      # 60 дней по $0.5: средняя ниже цены
    up = [1.0, 1.03, 1.06, 1.09, 1.12, 1.20, 1.30, 1.40]
    down = [1.0, 0.97, 0.94, 0.91, 0.88, 0.80, 0.70, 0.60]
    kw = dict(rally_exit=[[24, 0.10]], resume_drop_pct=0.10, crash_exit=[[24, 0.10]], resume_rise_pct=0.10)
    plain = Strategy("t", "p", 1000, 0.05, 0.05, **kw)
    trend = Strategy("t", "p", 1000, 0.05, 0.05, trend_ma_days=50, **kw)
    assert simulate(plain, pc, times, up, [0.0] * 8, NOCOST, 10, warm=high)["exits"] == 1
    r = simulate(trend, pc, times, up, [0.0] * 8, NOCOST, 10, warm=high)
    assert r["exits"] == 0 and r["mode"] == "lp"            # рост ниже средней — отскок, бот остаётся в пуле
    assert simulate(trend, pc, times, up, [0.0] * 8, NOCOST, 10, warm=low)["exits"] == 1   # рост в растущем рынке
    assert simulate(plain, pc, times, down, [0.0] * 8, NOCOST, 10, warm=low)["crashes"] == 1
    r = simulate(trend, pc, times, down, [0.0] * 8, NOCOST, 10, warm=low)
    assert r["crashes"] == 0 and r["mode"] == "lp"          # провал выше средней — бот остаётся в пуле
    assert simulate(trend, pc, times, down, [0.0] * 8, NOCOST, 10, warm=high)["crashes"] == 1
    assert simulate(trend, pc, times, up, [0.0] * 8, NOCOST, 10)["exits"] == 1    # без истории — как раньше


def test_phase_detector():
    """Фаза по дневным закрытиям: средняя за N дней и пороги с запасом; между порогами фаза не меняется;
    решение — только по закрытию дня; переживает перезапуск; смена настроек — пересчёт по закрытиям."""
    D, H = 86400.0, 23 * 3600.0                          # цены в 23:00 — последние цены дня
    ph = Phase(10, 0.05, 0.05)
    for d in range(8):
        ph.feed(d * D + 3600, 1.0)
        ph.feed(d * D + 80000, 1.0)                    # последняя цена дня — его закрытие
    assert ph.current() is None                          # 7 закрытых дней < 80% окна: фазы ещё нет
    ph.feed(8 * D + H, 1.0)
    ph.feed(9 * D + H, 1.0)
    assert ph.current() == "down" and ph.since == 7   # 8 закрытий, цена на средней — «падение/боковик»
    ph.feed(10 * D + H, 1.2)                                 # день 10 ещё не закрыт — фаза та же
    assert ph.current() == "down"
    ph.feed(11 * D + H, 1.2)                                 # закрытие 1.2 при средней ≈1.02: выше на 5%+ — рост
    assert ph.current() == "up" and ph.since == 10
    hi, lo = ph.bounds()
    assert math.isclose(lo, ph.ma() * 0.95)
    ph.feed(12 * D + H, 1.04)                                # закрытие 1.04 между порогами — фаза не меняется
    ph.feed(13 * D + H, 1.04)
    assert ph.current() == "up"
    for d in range(14, 18):
        ph.feed(d * D + H, 0.9)
    assert ph.current() == "down"
    again = Phase(10, 0.05, 0.05, state=ph.dump())       # перезапуск
    assert again.current() == "down" and again.closes == ph.closes and again.cur == ph.cur
    w = RallyWatch(None, {"phase": dict(ph.dump(), days=10, kind="sma", up=0.05, down=0.05)},
                   phase=dict(days=10, up=0.5, down=0.5))   # другие пороги — пересчёт заново по закрытиям
    assert w.phase() == "down" and w.ph.up == 0.5
    w2 = RallyWatch(None, phase=dict(days=10, up=0.05, down=0.05))
    w2.add(30 * D, 2.0)                                  # своя первая цена, затем предыстория из часовых цен
    w2.warm([(h * 3600.0, 1.0 if h < 24 * 25 else 1.5) for h in range(24 * 31)])
    assert w2.phase() == "up" and w2.ph.last()[0] == 29 and w2.ph.cur[:2] == (30, 2.0)   # день 30 ещё не закрыт
    assert not w2.need_warm() and w2.warm_days() == 20
    assert RallyWatch(None).phase() is None and not RallyWatch(None).need_warm()
    stale = Phase(10, 0.05, 0.05, state=ph.dump())       # бот видел цену утром и был выключен до конца дня
    stale.feed(30 * D + 3600, 5.0)
    stale.feed(33 * D + 3600, 5.0)
    assert stale.closes[-1][0] == 17 and stale.gap() and stale.current() == "down"   # утренняя цена — не закрытие
    sw = RallyWatch(None, phase=dict(days=10, up=0.05, down=0.05), state={"phase": dict(stale.dump(), days=10,
                    kind="sma", up=0.05, down=0.05)})
    assert sw.need_warm()                                # пропуск дней — догрузить закрытия из предыстории
    sw.warm([(h * 3600.0, 1.0 if h < 24 * 31 else 1.5) for h in range(24 * 33 + 2)])
    assert not sw.need_warm() and sw.ph.last()[0] == 32 and sw.phase() == "up"
    s = Strategy("t", "p", 1, 0.04, 0.04, phase_ma_days=60, crash_exit=[[72, 0.15]])
    w3 = watch_for(s)
    assert w3.ph.days == 60 and w3.ph.up == 0.05 and w3.ph.confirm == 2 and w3.drop_rules == [(72.0, 0.15)]
    two = Phase(10, 0.05, 0.05, confirm=2)                  # подтверждение: два закрытия подряд за порогом
    for d in range(10):
        two.feed(d * D + H, 1.0)
    two.feed(10 * D + H, 1.0)
    assert two.current() == "down"
    two.feed(11 * D + H, 1.3)
    two.feed(12 * D + H, 1.0)
    assert two.current() == "down" and two.run == [1, 0]    # одно закрытие выше порога — мало
    two.feed(13 * D + H, 1.3)                                   # однодневный выброс — фаза не меняется
    assert two.current() == "down" and two.run == [0, 0]
    two.feed(14 * D + H, 1.3)
    assert two.current() == "down" and two.run[0] == 1
    two.feed(15 * D + H, 1.3)                                   # второе закрытие подряд выше порога — рост
    assert two.current() == "up" and two.since == 14
    re = RallyWatch(None, {"phase": dict(two.dump(), days=10, kind="sma", up=0.05, down=0.05, confirm=1)},
                    phase=dict(days=10, up=0.05, down=0.05, confirm=2))
    assert re.phase() == "up" and re.ph.run == two.run       # смена confirm — пересчёт, тот же итог


def test_phase_bull_and_bear():
    """Фаза роста — снять пул и всё в SUI; фаза роста кончилась — снова пул; после выхода в USDC на падении
    фаза роста тоже переводит всё в SUI."""
    D = 86400.0
    pc = PoolCfg("p", "0x0")
    s = Strategy("t", "p", 1000, 0.04, 0.04, out_minutes=180, phase_ma_days=10, phase_up=0.05, phase_down=0.05,
                 phase_confirm=1, crash_exit=[[72, 0.15]], resume_rise_pct=0.10)
    w = watch_for(s)
    w.warm([(h * 3600.0, 1.0) for h in range(24 * 12)])           # 12 дней по $1 — фаза «падение/боковик»
    t = 12 * D + 22 * 3600                                         # 22:00 UTC — цена конца дня
    book = init_book(s, pc, st(1.0, t=t), NOCOST)
    w.add(t, 1.0)
    assert decide(book, s, st(1.0, t=t + 60), w) is None and book.mode == "lp"
    assert step(book, s, st(1.2, t=t + 3600), w, NOCOST) is None   # день ещё не закрыт — фаза прежняя
    assert book.mode == "lp" and book.out_since == t + 3600
    ev = step(book, s, st(1.2, t=t + D), w, NOCOST)                 # закрытие 1.2 — фаза роста
    assert ev[0] == "фаза роста" and book.mode == "up" and book.L == 0 and book.idle_a == 0   # a — USDC, b — SUI
    assert "средней за 10 дн." in ev[1] and book.phases[-1][1] == "up"
    sui = book.idle_b / 1e9
    assert step(book, s, st(1.5, t=t + 2 * D), w, NOCOST) is None and book.idle_b / 1e9 == sui   # держит SUI
    for k in range(3, 9):
        ev = step(book, s, st(0.8, t=t + k * D), w, NOCOST) or ev
    assert ev[0] == "конец фазы роста" and book.mode == "lp" and book.L > 0 and book.phases[-1][1] == "down"
    r = summary(book, st(0.8, t=t + 9 * D), no_price)
    assert r["mode"] == "lp" and abs(r["value_sui"] - sui) / sui < 0.01   # переход без потерь (без издержек)
    book.mode, book.L, book.peak = "usdc", 0.0, 0.8                 # вышли в USDC на падении
    book.idle_a, book.idle_b = 800e6, 0.0
    s2 = replace(s, resume_rise_pct=None)                           # без возврата по отскоку: только фаза
    for k in range(9, 16):
        ev = step(book, s2, st(1.3, t=t + k * D), w, NOCOST) or ev
    assert ev[0] == "фаза роста" and book.mode == "up" and book.idle_a == 0 and book.idle_b > 0
    ev = step(book, replace(s, phase_ma_days=None), st(1.3, t=t + 16 * D), w, NOCOST)   # стратегия без фаз
    assert ev[0] == "конец фазы роста" and book.mode == "lp" and book.L > 0              # не застревает в SUI


def test_phase_in_simulation():
    """На истории: долгий рост — бот почти весь путь в SUI (близко к «держать SUI»), без фаз пул сильно
    отстаёт; предыстория для фазы сдвигается на целые сутки."""
    D = 86400.0
    pc = PoolCfg("p", "0x0")
    times = [k * 3600.0 for k in range(24 * 60)]
    prices = [1.0 * 1.02 ** (k / 24) for k in range(24 * 60)]      # +2% в день 60 дней
    warm = [(-(40 * 24 - h) * 3600.0, 1.0) for h in range(40 * 24)]
    kw = dict(crash_exit=[[72, 0.15]], resume_rise_pct=0.10)
    plain = Strategy("t", "p", 1000, 0.04, 0.04, out_minutes=180, **kw)
    phase = Strategy("t", "p", 1000, 0.04, 0.04, out_minutes=180, phase_ma_days=20, **kw)
    a = simulate(plain, pc, times, prices, [0.0] * len(times), NOCOST, 10, warm=warm)
    b = simulate(phase, pc, times, prices, [0.0] * len(times), NOCOST, 10, warm=warm)
    assert b["mode"] == "up" and b["value_sui"] > 0.9 * 1000 and a["value_sui"] < 0.6 * 1000


def test_staking_and_leverage_in_growth_phase():
    """«Тень»: в фазе роста стейкинг лежащих SUI и лонг на фьючерсах (плечо 1.5× через фьючерс 2×): экспозиция
    1.5× капитала, плата за удержание из залога, ликвидация на глубоком падении, закрытие при конце роста."""
    D = 86400.0
    pc = PoolCfg("p", "0x0")
    s = Strategy("t", "p", 1000, 0.05, 0.05, up_leverage=1.5, perp_leverage=2, funding_apy=0.10, stake_apy=0.02)
    assert s.paper_only() == ["stake_apy", "up_leverage"] and Strategy("t", "p", 1).paper_only() == []
    book = init_book(s, pc, st(1.0, t=0.0), NOCOST)
    enter_up(book, st(1.0, t=0.0), NOCOST, s)
    spot = book.idle_b / 1e9                                      # a — USDC, b — SUI
    assert math.isclose(spot + book.perp_sui, 1500, rel_tol=1e-6) and math.isclose(book.perp_margin, 500, rel_tol=1e-6)
    assert math.isclose(summary(book, st(1.0), no_price)["value"], 1000, rel_tol=1e-6)
    assert math.isclose(summary(book, st(1.1), no_price)["value"], 1150, rel_tol=1e-6)   # +10% цены → +15% капитала
    carry(book, s, st(1.0, t=0.0))
    assert carry(book, s, st(1.0, t=365 * D)) is None            # год: плата 10% объёма, стейкинг 2% лежащих SUI
    assert math.isclose(book.perp_margin, 500 - 100, rel_tol=1e-6) and math.isclose(book.idle_b / 1e9, spot * 1.02, rel_tol=1e-6)
    assert math.isclose(book.staked_sui, spot * 0.02, rel_tol=1e-6) and math.isclose(book.funding_usd, 100, rel_tol=1e-6)
    ev = carry(book, s, st(0.62, t=365 * D + 60))                # падение −38% съедает залог — ликвидация
    assert ev[0] == "ликвидация" and book.perp_sui == 0 and len(book.liquidations) == 1
    assert math.isclose(summary(book, st(0.62), no_price)["value"], book.idle_b / 1e9 * 0.62, rel_tol=1e-6)
    book2 = init_book(s, pc, st(1.0, t=0.0), NOCOST)
    enter_up(book2, st(1.0, t=0.0), NOCOST, s)
    close_perp(book2, st(1.2, t=D), NOCOST)                       # конец роста: лонг закрыт, прибыль — в USDC
    assert book2.perp_sui == 0 and math.isclose(book2.idle_a / 1e6, 500 + 1000 * 0.2, rel_tol=1e-6)
    w = watch_for(Strategy("t", "p", 1, phase_ma_days=10, phase_confirm=1))
    w.warm([(h * 3600.0, 1.0 if h < 24 * 12 else 1.3) for h in range(24 * 15)])
    sp = replace(s, phase_ma_days=10, phase_confirm=1, out_minutes=180)
    b3 = init_book(sp, pc, st(1.3, t=15 * D), NOCOST)
    ev = step(b3, sp, st(1.3, t=15 * D + 60), w, NOCOST)          # фаза роста: всё в SUI и лонг
    assert ev[0] == "фаза роста" and "лонг" in ev[1] and b3.perp_sui > 0
    for k in range(16, 22):
        ev = step(b3, sp, st(0.9, t=k * D + 80000), w, NOCOST) or ev
    assert ev[0] == "конец фазы роста" and b3.perp_sui == 0 and b3.mode == "lp" and b3.L > 0


if __name__ == "__main__":
    tests = [(n, f) for n, f in globals().items() if n.startswith("test_")]
    for n, f in tests:
        f()
        print("ok", n)
    print(f"все проверки пройдены: {len(tests)}")
