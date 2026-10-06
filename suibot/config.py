"""Настройки бота из TOML-файла (по умолчанию suibot.toml в корне репозитория)."""
from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from lpscan.common import ROOT
from suibot.book import Costs
from suibot.chain import PoolCfg
from suibot.strategy import Strategy


@dataclass
class LiveCfg:
    """Боевой режим: какая стратегия ведётся на реальные деньги и ограничения."""
    strategy: str
    max_capital_usd: float = 200.0      # бот использует не больше этой суммы из кошелька
    gas_reserve_sui: float = 1.0        # столько SUI всегда остаётся в кошельке на газ
    slippage: float = 0.005             # допустимое проскальзывание обмена через агрегатор
    price_band: float = 0.0015          # на сколько цена может сдвинуться, пока открывается/закрывается позиция
    min_swap_usd: float = 1.0           # меньшие обмены не делаются
    dry_run: bool = True                # true — только симуляция, ничего не отправляется
    address: str | None = None          # адрес кошелька для симуляции без ключа


@dataclass
class Config:
    pools: dict[str, PoolCfg]
    strategies: list[Strategy]
    costs: Costs = field(default_factory=Costs)
    poll_seconds: int = 30
    snapshot_minutes: int = 15
    report_hour_utc: int = 6
    staking_apy: float = 0.014          # стейкинг SUI у валидаторов — ориентир «сколько SUI без риска»
    state_dir: Path = ROOT / "data" / "private" / "bot"
    live: LiveCfg | None = None

    def pools_used(self) -> dict[str, PoolCfg]:
        return {s.pool: self.pools[s.pool] for s in self.strategies}


def load(path: str | Path) -> Config:
    raw = tomllib.loads(Path(path).read_text())
    pools = {k: PoolCfg(key=k, **v) for k, v in raw.pop("pools").items()}
    strategies = [Strategy(**s) for s in raw.pop("strategy")]
    costs = Costs(**raw.pop("costs", {}))
    live = LiveCfg(**raw.pop("live")) if "live" in raw else None
    names = [s.name for s in strategies]
    if len(set(names)) != len(names):
        raise SystemExit("названия стратегий должны быть разными")
    for s in strategies:
        if s.pool not in pools:
            raise SystemExit(f"стратегия «{s.name}»: нет пула {s.pool} в [pools]")
        if s.rebalance not in ("both", "down", "up", "none"):
            raise SystemExit(f"стратегия «{s.name}»: rebalance должен быть both, down, up или none")
    if "state_dir" in raw:
        sd = Path(raw.pop("state_dir"))
        raw["state_dir"] = sd if sd.is_absolute() else ROOT / sd
    if live and live.strategy not in names:
        raise SystemExit(f"[live] strategy: нет стратегии «{live.strategy}» в списке [[strategy]]")
    return Config(pools=pools, strategies=strategies, costs=costs, live=live, **raw)
