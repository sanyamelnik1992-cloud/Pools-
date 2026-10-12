"""Проверки тени фьючерса «пробой 20 дн.» без сети:  python3 tests/test_perp_shadow.py  (или pytest).
Тень работает только на бумаге: здесь проверяем сигнал по закрытому дню, вход на следующий день, стопы,
перенос цели и трейлинг, сохранение состояния и то, что тень не трогает деньги."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from suibot.perp_shadow import PerpCfg, PerpShadow  # noqa: E402

DAY = 86400.0


def feed_days(sh, days, base=1.0, swing=0.02, t0=0.0):
    """Дни с колебанием ±swing внутри дня (даёт ATR) и закрытием на base."""
    for d in range(days):
        for k in range(24):
            p = base + (swing if k % 2 else -swing)
            sh.on_price(t0 + d * DAY + k * 3600, p)


def test_breakout_enters_next_day_not_same_day():
    """Пробой виден только по закрытому дню: вход — на первой цене следующего дня, не в день пробоя."""
    sh = PerpShadow(PerpCfg())
    feed_days(sh, 25)
    for k in range(24):                                 # день 25: цена 1.20 весь день — пробой
        sh.on_price(25 * DAY + k * 3600, 1.20)
    assert sh.pos is None                               # в день пробоя позиции нет: день ещё не закрыт
    sh.on_price(26 * DAY, 1.21)                         # первая цена следующего дня: день 25 закрыт — вход
    assert sh.pos is not None and sh.pos["side"] == 1 and abs(sh.pos["entry"] - 1.21 * 1.0005) < 1e-9


def test_full_capital_has_no_leverage():
    """Весь капитал: номинал не больше капитала (max_lev 1), без плеча и без ликвидации."""
    sh = PerpShadow(PerpCfg(capital_usd=200.0))
    feed_days(sh, 25)
    for k in range(24):
        sh.on_price(25 * DAY + k * 3600, 1.20)
    sh.on_price(26 * DAY, 1.21)
    assert sh.pos is not None and sh.pos["lev"] <= 1.0 + 1e-9


def test_stop_loss_closes_position_with_loss():
    sh = PerpCfg()
    sh = PerpShadow(sh)
    feed_days(sh, 25)
    for k in range(24):
        sh.on_price(25 * DAY + k * 3600, 1.20)
    sh.on_price(26 * DAY, 1.21)
    stop = sh.pos["stop"]
    sh.on_price(26 * DAY + 3600, stop - 0.01)            # цена ниже стопа
    assert sh.pos is None and sh.E < 200.0 and sh.trades and sh.trades[-1]["pnl"] < 0


def test_half_at_plus_2r_then_stop_to_plus_1r():
    sh = PerpShadow(PerpCfg())
    feed_days(sh, 25)
    for k in range(24):
        sh.on_price(25 * DAY + k * 3600, 1.20)
    sh.on_price(26 * DAY, 1.21)
    qty0, entry, R = sh.pos["qty"], sh.pos["entry"], sh.pos["R"]
    sh.on_price(26 * DAY + 3600, entry + 2 * R + 0.001)   # +2R
    assert sh.pos is not None and sh.pos["tp1"] is True
    assert abs(sh.pos["qty"] - qty0 * 0.5) < 1e-6 * qty0
    assert abs(sh.pos["stop"] - (entry + R)) < 1e-9       # стоп остатка — на +1R


def test_trailing_moves_up_only():
    sh = PerpShadow(PerpCfg())
    feed_days(sh, 25)
    for k in range(24):
        sh.on_price(25 * DAY + k * 3600, 1.20)
    sh.on_price(26 * DAY, 1.21)
    entry, R = sh.pos["entry"], sh.pos["R"]
    sh.on_price(26 * DAY + 3600, entry + 2 * R + 0.001)
    s1 = sh.pos["stop"]
    sh.on_price(26 * DAY + 7200, entry + 3 * R)           # цена выросла — стоп поднимается
    s2 = sh.pos["stop"]
    sh.on_price(26 * DAY + 10800, entry + 2.5 * R)        # откат — стоп не опускается
    assert s2 > s1 and sh.pos["stop"] == s2


def test_state_roundtrip_keeps_position():
    sh = PerpShadow(PerpCfg())
    feed_days(sh, 25)
    for k in range(24):
        sh.on_price(25 * DAY + k * 3600, 1.20)
    sh.on_price(26 * DAY, 1.21)
    saved = json.loads(json.dumps(sh.to_dict()))         # как в состоянии бумажного режима
    again = PerpShadow(PerpCfg(), saved)
    assert again.pos["side"] == sh.pos["side"] and abs(again.E - sh.E) < 1e-9
    assert again.summary()["equity"] == sh.summary()["equity"]


def test_warm_from_daily_history_signals_at_once():
    """Прогрев дневными свечами: сигнал работает сразу, без трёх недель ожидания; повтор дней не дублирует историю."""
    bars = []
    for d in range(30):
        bars.append((d, 1.0, 1.01, 0.99, 1.0 + (0.01 if d % 2 else -0.01)))
    sh = PerpShadow(PerpCfg())
    assert sh.need_warm()
    assert sh.warm(bars) == 30 and not sh.need_warm()
    assert sh.warm(bars) == 0                              # те же дни второй раз — не добавляются
    for k in range(24):                                    # день 30: пробой вверх
        sh.on_price(30 * DAY + k * 3600, 1.20)
    sh.on_price(31 * DAY, 1.21)
    assert sh.pos is not None and sh.pos["side"] == 1


def test_no_signal_inside_channel():
    sh = PerpShadow(PerpCfg())
    feed_days(sh, 30)                                    # колебание 0.98–1.02, пробоя нет
    assert sh.pos is None and sh.pending is None


def test_config_pool_must_be_read_by_a_strategy():
    """Тень читает пул, который есть в стратегиях; иначе конфиг не загрузится."""
    from suibot.config import load
    cfg = load(Path(__file__).resolve().parent.parent / "suibot.toml")
    assert cfg.perp is not None and cfg.perp.pool in {s.pool for s in cfg.strategies}
    assert cfg.live is not None and cfg.live.dry_run in (True, False)


if __name__ == "__main__":
    tests = [(n, f) for n, f in globals().items() if n.startswith("test_")]
    for n, f in tests:
        f()
        print("ok", n)
    print(f"все проверки пройдены: {len(tests)}")
