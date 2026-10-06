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
class Config:
    pools: dict[str, PoolCfg]
    strategies: list[Strategy]
    costs: Costs = field(default_factory=Costs)
    poll_seconds: int = 30
    snapshot_minutes: int = 15
    report_hour_utc: int = 6
    state_dir: Path = ROOT / "data" / "private" / "bot"

    def pools_used(self) -> dict[str, PoolCfg]:
        return {s.pool: self.pools[s.pool] for s in self.strategies}


def load(path: str | Path) -> Config:
    raw = tomllib.loads(Path(path).read_text())
    pools = {k: PoolCfg(key=k, **v) for k, v in raw.pop("pools").items()}
    strategies = [Strategy(**s) for s in raw.pop("strategy")]
    costs = Costs(**raw.pop("costs", {}))
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
    return Config(pools=pools, strategies=strategies, costs=costs, **raw)
