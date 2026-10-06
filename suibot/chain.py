"""Данные сети Sui для бота: состояние пулов (Sui GraphQL) и цены токенов наград (DefiLlama)."""
from __future__ import annotations

from dataclasses import dataclass

from lpscan.common import get_json
from sui_pools import SUI, USDC, gql, parse, ts


@dataclass
class PoolCfg:
    key: str
    object: str
    dex: str = "cetus"
    a_is_sui: bool = False   # у Cetus USDC/SUI монета a — USDC


def usd_to_raw(p_usd: float, a_is_sui: bool) -> float:
    """Цена SUI в $ → сырая цена пула (мин. единиц b за мин. единицу a); SUI 9 знаков, USDC 6."""
    return p_usd / 1e3 if a_is_sui else 1e3 / p_usd


def raw_to_usd(p_raw: float, a_is_sui: bool) -> float:
    return p_raw * 1e3 if a_is_sui else 1e3 / p_raw


def unit_usd(p_usd: float, a_is_sui: bool) -> tuple[float, float]:
    """$ за минимальную единицу монет a и b."""
    return (p_usd / 1e9, 1e-6) if a_is_sui else (1e-6, p_usd / 1e9)


def state_from_price(p_usd: float, a_is_sui: bool) -> dict:
    """Состояние пула по одной цене — для проверки на истории, где есть только свечи."""
    ua, ub = unit_usd(p_usd, a_is_sui)
    return {"sq": usd_to_raw(p_usd, a_is_sui) ** 0.5, "sui": p_usd, "ua": ua, "ub": ub}


def read_pools(pools: dict[str, PoolCfg]) -> dict[str, dict]:
    """Текущее состояние пулов одним запросом: цена, счётчики комиссий и наград, шаг тиков, время чекпоинта."""
    keys = list(pools)
    d = gql("{ checkpoint { timestamp } " + " ".join(
        f'p{i}: object(address:"{pools[k].object}"){{ asMoveObject {{ contents {{ json }} }} }}'
        for i, k in enumerate(keys)) + "}")
    t = ts(d["checkpoint"]["timestamp"])
    out = {}
    for i, k in enumerate(keys):
        js = d[f"p{i}"]["asMoveObject"]["contents"]["json"]
        st = parse(js, pools[k].dex, pools[k].a_is_sui)
        st.pop("rewards_raw", None)
        st["t"] = t
        st["spacing"] = int(js.get("tick_spacing") or js["ticks_manager"]["tick_spacing"])
        out[k] = st
    return out


def token_price(t: str, sui_usd: float) -> tuple[float, int]:
    """Цена и число знаков токена награды: SUI — по цене пула, USDC — $1, прочие — DefiLlama."""
    if t == SUI:
        return sui_usd, 9
    if t == USDC:
        return 1.0, 6
    try:
        c = get_json(f"https://coins.llama.fi/prices/current/sui:{t}", cache_ttl=600)["coins"].get(f"sui:{t}")
    except RuntimeError:
        c = None
    return (c["price"], c.get("decimals", 9)) if c else (0.0, 9)
