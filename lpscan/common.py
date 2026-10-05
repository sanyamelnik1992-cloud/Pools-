"""Общие утилиты: HTTP с ретраями и кэшем, конфигурация, классификация токенов."""
from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
RAW = DATA / "raw"
PROC = DATA / "processed"
CACHE = DATA / ".cache"

KRYSTAL_BASE = "https://cloud-api.krystal.app"
LLAMA_YIELDS = "https://yields.llama.fi"
LLAMA_API = "https://api.llama.fi"
GECKO = "https://api.geckoterminal.com/api/v2"

CHAINS = {
    42161: {"name": "Arbitrum", "llama": "Arbitrum", "gecko": "arbitrum",
            "rpc": "https://arb1.arbitrum.io/rpc"},
    8453: {"name": "Base", "llama": "Base", "gecko": "base",
           "rpc": "https://base-rpc.publicnode.com"},
}
# Blast — только для сравнения: Krystal его не поддерживает.
BLAST = {"name": "Blast", "llama": "Blast", "gecko": "blast", "rpc": "https://rpc.blast.io"}

# --- Классификация токенов -------------------------------------------------
STABLES = {
    "USDC", "USDC.E", "USDBC", "USDT", "USD₮0", "USDT0", "DAI", "USDS", "FRAX", "LUSD",
    "GHO", "USDE", "SUSDE", "EURC", "USDB", "AXLUSDC", "PYUSD", "CRVUSD", "USDZ",
    "USD+", "OUSD", "RLUSD", "FDUSD", "TUSD", "DOLA", "MIM", "USDA", "SUSDS", "USDM",
    "EUSD", "BOLD", "MSUSD", "USR", "AUSD", "USDF", "FRXUSD", "SCUSD", "USDG", "USD1",
}
EUR_STABLES = {"EURC", "EURA", "EURE"}
ETH_LIKE = {
    "ETH", "WETH", "WSTETH", "STETH", "CBETH", "RETH", "WEETH", "EZETH", "RSETH",
    "WRSETH", "SUPEROETHB", "OETH", "WOETH", "SFRXETH", "FRXETH", "METH", "ETHX",
    "SUPEROETH", "WSUPEROETHB", "AGETH", "PZETH", "OSETH", "TETH", "YETH",
}
BTC_LIKE = {"WBTC", "CBBTC", "TBTC", "LBTC", "SOLVBTC", "BTC", "UBTC", "FBTC",
            "EBTC", "UNIBTC", "SOLVBTC.BBN", "CBBTC.E", "BTC.B", "BTCB"}
# Крупные ликвидные альты (не мемы): риск выше ETH/BTC, но токены «настоящие».
MAJORS = {"ARB", "AERO", "GMX", "LINK", "UNI", "PENDLE", "VIRTUAL", "OP", "CRV", "AAVE",
          "ZRO", "CAKE", "SUSHI", "GRAIL", "MORPHO", "COMP", "LDO", "WLD", "SOL", "ZORA",
          "BRETT", "DEGEN", "WELL", "EIGEN", "MAGIC", "RDNT", "XAI", "ENA", "PYTH", "TIA",
          "VVV", "KAITO", "CLANKER", "TRUMP", "DOT", "ICP", "IOTA", "SPX", "TOSHI"}


def norm_sym(s: str) -> str:
    return (s or "").upper().replace("₮", "T").strip()


STOCK_NAME_HINTS = (" Inc", "Corporation", "Corp.", "& Co", "Holdings")


def token_class(sym: str, name: str | None = None) -> str:
    s = norm_sym(sym)
    if name and any(h in name for h in STOCK_NAME_HINTS) and s.endswith("C"):
        return "stock"
    if s in {norm_sym(x) for x in STABLES}:
        return "stable"
    if s in ETH_LIKE:
        return "eth"
    if s in BTC_LIKE:
        return "btc"
    if s in MAJORS:
        return "major"
    return "alt"


def pair_category(sym0: str, sym1: str, name0: str | None = None, name1: str | None = None) -> str:
    """Категория пары по классам токенов (определяет профиль риска IL)."""
    a, b = sorted([token_class(sym0, name0), token_class(sym1, name1)])
    if "stock" in (a, b):
        return "Токенизир. акции"
    if a == b == "stable":
        return "Стейбл/стейбл"
    if a == b and a in ("eth", "btc"):
        return "Коррелир. (ETH/LST, BTC/BTC)"
    if {a, b} == {"btc", "eth"}:
        return "ETH/BTC"
    if b == "stable" and a in ("btc", "eth") or a == "stable" and b in ("btc", "eth"):
        return "Голубая фишка/стейбл"
    if "alt" in (a, b):
        return "Альт/мем (long-tail)"
    return "Крупный альт"


# --- HTTP ------------------------------------------------------------------
_session = requests.Session()
_session.headers["User-Agent"] = "lpscan/1.0 (+research)"
_last_call: dict[str, float] = {}


def _throttle(host: str, min_interval: float):
    t = _last_call.get(host, 0)
    wait = min_interval - (time.time() - t)
    if wait > 0:
        time.sleep(wait)
    _last_call[host] = time.time()


def get_json(url: str, params: dict | None = None, headers: dict | None = None,
             cache_ttl: float = 6 * 3600, min_interval: float = 0.15, retries: int = 5):
    """GET с ретраями (экспоненциальная пауза), троттлингом по хосту и файловым кэшем."""
    key = hashlib.sha1(json.dumps([url, params], sort_keys=True).encode()).hexdigest()
    cpath = CACHE / f"{key}.json"
    if cache_ttl and cpath.exists() and time.time() - cpath.stat().st_mtime < cache_ttl:
        return json.loads(cpath.read_text())
    host = url.split("/")[2]
    last_err = None
    for attempt in range(retries):
        _throttle(host, min_interval)
        try:
            r = _session.get(url, params=params, headers=headers, timeout=60)
            if r.status_code == 429 or r.status_code >= 500:
                last_err = f"HTTP {r.status_code}"
                time.sleep(2 ** attempt * (3 if "gecko" in host else 1))
                continue
            r.raise_for_status()
            data = r.json()
            CACHE.mkdir(parents=True, exist_ok=True)
            cpath.write_text(json.dumps(data))
            return data
        except (requests.RequestException, ValueError) as e:  # noqa: PERF203
            last_err = str(e)
            time.sleep(2 ** attempt)
    raise RuntimeError(f"GET {url} {params} failed: {last_err}")


def rpc_call(rpc: str, method: str, params: list):
    for attempt in range(4):
        try:
            r = _session.post(rpc, json={"jsonrpc": "2.0", "id": 1, "method": method,
                                         "params": params}, timeout=30)
            j = r.json()
            if "result" in j:
                return j["result"]
            last = j.get("error")
        except (requests.RequestException, ValueError) as e:
            last = str(e)
        time.sleep(2 ** attempt)
    raise RuntimeError(f"RPC {method} failed: {last}")


def krystal_headers() -> dict:
    key = os.environ.get("KRYSTAL_CLOUD_KEY")
    if not key:
        raise SystemExit("Нужна переменная окружения KRYSTAL_CLOUD_KEY")
    return {"KC-APIKey": key}


def save(path: Path, obj, compact: bool = False):
    path.parent.mkdir(parents=True, exist_ok=True)
    if compact:
        path.write_text(json.dumps(obj, ensure_ascii=False, separators=(",", ":")))
    else:
        path.write_text(json.dumps(obj, ensure_ascii=False, indent=1))


def load(path: Path):
    return json.loads(Path(path).read_text())
