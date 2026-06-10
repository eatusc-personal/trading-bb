"""Peer-group divergence scanner.

Detects stocks that are NOT moving with their peer group: e.g. all semiconductor
names crashed yesterday but two of them held up. For each scanned set (a curated
industry group, a custom list of stocks/ETFs, or sector ETFs vs SPY) it computes:

- window returns (1d / 5d / 20d by default) for every member,
- a robust z-score per member per window: leave-one-out median/MAD of the peer
  returns, so one big outlier can't hide itself by dragging the group stats,
- a 60-day correlation and beta of each member vs the equal-weight peer index
  (excluding itself) — a divergence is only interesting when the name normally
  tracks the group, so low-correlation members are reported but not flagged,
- a plain-English label ("Held up while group fell", "Lagged group rally", ...).

Peer groups are stored in an editable JSON file (peer_groups.json next to this
module). The built-in seed below is written there on first run; after that the
file is the source of truth and can be edited through the API/UI or by hand.

GET  /api/divergence/groups            list groups (grouped by category in UI)
POST /api/divergence/groups            create a group
PUT  /api/divergence/groups/{id}       update a group
DELETE /api/divergence/groups/{id}     delete a group
GET  /api/divergence/overview          1-day "pulse" per group from batched quotes
POST /api/divergence/scan              full divergence scan

Price data comes through main.fetch_history / main.fetch_quote, so it shares the
provider router, the in-memory cache and the per-provider budget guard. History
is cached for 30 minutes here — divergence works on daily closes, so there is no
reason to re-ping providers more often than that.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from statistics import median as stat_median
from typing import Any

import pandas as pd
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

logger = logging.getLogger("openbb_api.divergence")

router = APIRouter()

HISTORY_TTL_SEC = 1800
CORR_LOOKBACK_DAYS = 60
MAX_SYMBOLS_PER_SCAN = 80
DEFAULT_WINDOWS = [1, 5, 20]

GROUPS_FILE = os.path.join(os.path.dirname(__file__), "peer_groups.json")
_groups_lock = threading.Lock()
_groups_cache: tuple[float, list[dict[str, Any]]] | None = None  # (mtime, groups)

SEED_GROUPS: list[dict[str, Any]] = [
    # ----- Industries -----
    {
        "id": "semiconductors",
        "name": "Semiconductors",
        "category": "Industries",
        "benchmark": "SMH",
        "members": ["NVDA", "AMD", "AVGO", "TSM", "INTC", "MU", "QCOM", "TXN", "MRVL", "ON", "AMAT", "ASML"],
    },
    {
        "id": "software",
        "name": "Software / SaaS",
        "category": "Industries",
        "benchmark": "IGV",
        "members": ["MSFT", "CRM", "ORCL", "ADBE", "NOW", "SNOW", "PLTR", "INTU", "PANW", "CRWD", "DDOG", "TEAM"],
    },
    {
        "id": "mega_tech",
        "name": "Mega-cap Tech",
        "category": "Industries",
        "benchmark": "QQQ",
        "members": ["AAPL", "MSFT", "GOOGL", "AMZN", "META", "NVDA", "TSLA", "NFLX"],
    },
    {
        "id": "banks",
        "name": "Banks / Brokers",
        "category": "Industries",
        "benchmark": "XLF",
        "members": ["JPM", "BAC", "WFC", "C", "GS", "MS", "USB", "PNC", "TFC", "SCHW"],
    },
    {
        "id": "energy",
        "name": "Energy",
        "category": "Industries",
        "benchmark": "XLE",
        "members": ["XOM", "CVX", "COP", "SLB", "EOG", "MPC", "PSX", "VLO", "OXY", "HAL"],
    },
    {
        "id": "biotech",
        "name": "Biotech",
        "category": "Industries",
        "benchmark": "XBI",
        "members": ["AMGN", "GILD", "VRTX", "REGN", "MRNA", "BIIB", "ALNY", "ILMN"],
    },
    {
        "id": "pharma_healthcare",
        "name": "Pharma / Healthcare",
        "category": "Industries",
        "benchmark": "XLV",
        "members": ["LLY", "JNJ", "PFE", "MRK", "ABBV", "BMY", "UNH", "TMO", "ABT"],
    },
    {
        "id": "retail",
        "name": "Retail",
        "category": "Industries",
        "benchmark": "XRT",
        "members": ["WMT", "TGT", "COST", "HD", "LOW", "TJX", "ROST", "DG", "BBY"],
    },
    {
        "id": "airlines",
        "name": "Airlines",
        "category": "Industries",
        "benchmark": "JETS",
        "members": ["DAL", "UAL", "AAL", "LUV", "ALK", "JBLU"],
    },
    {
        "id": "payments_fintech",
        "name": "Payments / Fintech",
        "category": "Industries",
        "benchmark": "XLF",
        "members": ["V", "MA", "AXP", "PYPL", "COIN", "HOOD", "SOFI", "AFRM"],
    },
    {
        "id": "autos_ev",
        "name": "Autos / EV",
        "category": "Industries",
        "benchmark": "CARZ",
        "members": ["TSLA", "RIVN", "LCID", "GM", "F", "NIO", "XPEV", "LI"],
    },
    {
        "id": "gold_miners",
        "name": "Gold Miners",
        "category": "Industries",
        "benchmark": "GDX",
        "members": ["NEM", "GOLD", "AEM", "KGC", "AU", "WPM", "FNV"],
    },
    {
        "id": "defense",
        "name": "Defense / Aerospace",
        "category": "Industries",
        "benchmark": "ITA",
        "members": ["LMT", "RTX", "NOC", "GD", "BA", "LHX", "HII"],
    },
    {
        "id": "reits",
        "name": "REITs",
        "category": "Industries",
        "benchmark": "XLRE",
        "members": ["PLD", "AMT", "EQIX", "SPG", "O", "PSA", "CCI", "DLR"],
    },
    {
        "id": "utilities",
        "name": "Utilities",
        "category": "Industries",
        "benchmark": "XLU",
        "members": ["NEE", "DUK", "SO", "D", "AEP", "EXC", "SRE", "XEL"],
    },
    {
        "id": "sector_etfs",
        "name": "S&P Sector ETFs",
        "category": "Industries",
        "benchmark": "SPY",
        "members": ["XLK", "XLF", "XLE", "XLV", "XLI", "XLP", "XLY", "XLU", "XLB", "XLRE", "XLC"],
    },
    # ----- Crypto -----
    {
        "id": "btc_miners",
        "name": "Bitcoin Miners / AI Pivot",
        "category": "Crypto",
        "benchmark": "WGMI",
        "members": ["MARA", "RIOT", "CLSK", "CORZ", "IREN", "CIFR", "WULF", "HUT", "BTDR", "HIVE", "BITF", "BTBT"],
        "notes": "Trade like leveraged BTC plus power/data-center infrastructure plays.",
    },
    {
        "id": "crypto_treasury",
        "name": "Crypto Treasury / Balance Sheet",
        "category": "Crypto",
        "benchmark": "IBIT",
        "members": ["MSTR", "SMLR", "GLXY", "BKKT"],
        "notes": "Holding crypto or crypto-linked balance-sheet exposure. MSTR best known; SMLR/BKKT speculative.",
    },
    {
        "id": "crypto_exchanges",
        "name": "Crypto Exchanges / Fin Infra",
        "category": "Crypto",
        "benchmark": "ARKF",
        "members": ["COIN", "HOOD", "PYPL", "XYZ", "NU", "CME", "IBKR"],
        "notes": "Exchanges, brokers, and crypto financial infrastructure. XYZ = Block (formerly SQ).",
    },
    {
        "id": "crypto_infra_hw",
        "name": "Crypto-adjacent Hardware / DC",
        "category": "Crypto",
        "benchmark": "SMH",
        "members": ["NVDA", "AMD", "SMCI", "APLD", "VRT", "ETN"],
        "notes": "Less pure crypto; benefits when mining, AI hosting, and data-center power demand overlap.",
    },
    # ----- Defense & Drones -----
    {
        "id": "drones_tactical",
        "name": "Small / Tactical Drones",
        "category": "Defense & Drones",
        "benchmark": "ITA",
        "members": ["RCAT", "ONDS", "AVAV", "KTOS", "UMAC", "PDYN", "OPTT"],
        "notes": "RCAT Black Widow (Army SRR), ONDS counter-UAS/Iron Drone. UMAC/PDYN/OPTT very speculative.",
    },
    {
        "id": "counter_uas",
        "name": "Counter-UAS / Air Defense",
        "category": "Defense & Drones",
        "benchmark": "ITA",
        "members": ["ONDS", "AVAV", "DRS", "KTOS", "RTX", "LMT", "NOC"],
        "notes": "Counter-drone, directed energy, air defense. AVAV Freedom Eagle C-UAS missile.",
    },
    {
        "id": "naval_unmanned",
        "name": "Naval / Unmanned Maritime",
        "category": "Defense & Drones",
        "benchmark": "ITA",
        "members": ["OPTT", "KITT", "TXT", "LHX", "HII", "GD"],
        "notes": "Few public pure plays. Watch private: Saronic, Anduril, Shield AI, Skydio, Epirus, Helsing.",
    },
    {
        "id": "defense_electronics",
        "name": "Defense Electronics / RF / EW",
        "category": "Defense & Drones",
        "benchmark": "ITA",
        "members": ["MRCY", "DRS", "VSEC", "HEI", "TDG", "CW", "ATRO", "MOG-A"],
        "notes": "Picks-and-shovels: sensors, RF, electronic warfare, aerospace components.",
    },
    # ----- Semis & Photonics -----
    {
        "id": "photonics_optics",
        "name": "Photonics / AI Data-center Optics",
        "category": "Semis & Photonics",
        "benchmark": "SMH",
        "members": ["COHR", "LITE", "CIEN", "GLW", "FN", "AAOI", "POET", "LWLG", "IPGP"],
        "notes": "Next layer of AI infrastructure. NVDA reportedly investing in LITE/COHR photonics.",
    },
    {
        "id": "silicon_photonics",
        "name": "Silicon Photonics / Interconnect",
        "category": "Semis & Photonics",
        "benchmark": "SMH",
        "members": ["POET", "LWLG", "COHR", "LITE", "AAOI", "FN", "MRVL", "AVGO"],
        "notes": "Optical engines, interposers, transceivers. LWLG very speculative.",
    },
    {
        "id": "power_semis",
        "name": "GaN / SiC Power Semis",
        "category": "Semis & Photonics",
        "benchmark": "SMH",
        "members": ["NVTS", "WOLF", "POWI", "AXTI", "AEHR", "ON", "STM", "IFNNY"],
        "notes": "AI data-center power, EV, grid electrification. WOLF distressed/speculative.",
    },
    {
        "id": "rf_compound_semis",
        "name": "RF / Compound Semis",
        "category": "Semis & Photonics",
        "benchmark": "SMH",
        "members": ["MTSI", "QRVO", "SWKS", "ADI", "TXN", "AXTI", "VSH", "IPGP"],
        "notes": "Defense comms, satellites, 5G, radar, EW overlap.",
    },
    {
        "id": "semicap_niche",
        "name": "Semi Equipment / Test (Niche)",
        "category": "Semis & Photonics",
        "benchmark": "SMH",
        "members": ["AEHR", "ACMR", "ONTO", "KLIC", "VECO", "CAMT", "FORM", "COHU"],
        "notes": "Smaller equipment/test names: wafer test, cleaning, inspection, packaging tools.",
    },
    {
        "id": "adv_packaging",
        "name": "Advanced Packaging / Chiplets",
        "category": "Semis & Photonics",
        "benchmark": "SMH",
        "members": ["AMKR", "CAMT", "KLIC", "ONTO", "TER", "FORM", "BESI.AS"],
        "notes": "Hybrid bonding, probe cards, metrology. BESI.AS is Amsterdam-listed (data may be spotty).",
    },
    # ----- My Buckets -----
    {
        "id": "basket_crypto",
        "name": "Bucket: Crypto",
        "category": "My Buckets",
        "benchmark": "IBIT",
        "members": ["MARA", "RIOT", "CLSK", "CORZ", "IREN", "CIFR", "WULF", "HUT", "BTDR", "COIN", "MSTR", "GLXY"],
    },
    {
        "id": "basket_drones",
        "name": "Bucket: Drones / Defense",
        "category": "My Buckets",
        "benchmark": "ITA",
        "members": ["RCAT", "ONDS", "AVAV", "KTOS", "DRS", "MRCY", "PDYN", "UMAC", "OPTT", "KITT"],
    },
    {
        "id": "basket_photonics",
        "name": "Bucket: Photonics / AI Optics",
        "category": "My Buckets",
        "benchmark": "SMH",
        "members": ["COHR", "LITE", "CIEN", "FN", "AAOI", "POET", "LWLG", "GLW", "MRVL"],
    },
    {
        "id": "basket_specialty_semis",
        "name": "Bucket: Small Specialty Semis",
        "category": "My Buckets",
        "benchmark": "SMH",
        "members": ["NVTS", "WOLF", "AEHR", "AXTI", "MTSI", "ACMR", "ONTO", "KLIC", "VECO", "CAMT"],
    },
    {
        "id": "basket_defense_semi",
        "name": "Bucket: Defense × Semi",
        "category": "My Buckets",
        "benchmark": "ITA",
        "members": ["MTSI", "QRVO", "ADI", "MRCY", "DRS", "KTOS", "AVAV", "LHX", "HEI", "CW"],
    },
]

CATEGORY_ORDER = ["Industries", "Crypto", "Defense & Drones", "Semis & Photonics", "My Buckets", "Custom"]

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slugify(name: str) -> str:
    return _SLUG_RE.sub("_", name.strip().lower()).strip("_")[:48] or "group"


def _normalize_group(raw: dict[str, Any]) -> dict[str, Any] | None:
    group_id = _slugify(str(raw.get("id") or raw.get("name") or ""))
    name = str(raw.get("name") or "").strip()
    if not group_id or not name:
        return None
    members: list[str] = []
    for sym in raw.get("members") or []:
        s = str(sym or "").upper().strip()
        if s and s not in members:
            members.append(s)
    benchmark = str(raw.get("benchmark") or "").upper().strip() or None
    category = str(raw.get("category") or "Custom").strip() or "Custom"
    notes = str(raw.get("notes") or "").strip()
    return {
        "id": group_id,
        "name": name,
        "category": category,
        "benchmark": benchmark,
        "members": members,
        **({"notes": notes} if notes else {}),
    }


def _write_groups_file(groups: list[dict[str, Any]]) -> None:
    tmp = GROUPS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"groups": groups}, fh, indent=2)
    os.replace(tmp, GROUPS_FILE)


def load_groups() -> list[dict[str, Any]]:
    """Load groups from the JSON file (mtime-cached); seed the file on first run."""
    global _groups_cache
    with _groups_lock:
        if not os.path.exists(GROUPS_FILE):
            _write_groups_file(SEED_GROUPS)
            _groups_cache = (os.path.getmtime(GROUPS_FILE), list(SEED_GROUPS))
            logger.info("Seeded %s with %d peer groups", GROUPS_FILE, len(SEED_GROUPS))
            return list(SEED_GROUPS)
        mtime = os.path.getmtime(GROUPS_FILE)
        if _groups_cache and _groups_cache[0] == mtime:
            return list(_groups_cache[1])
        try:
            with open(GROUPS_FILE, encoding="utf-8") as fh:
                raw = json.load(fh)
            entries = raw.get("groups") if isinstance(raw, dict) else raw
            groups = [g for g in (_normalize_group(e) for e in entries or [] if isinstance(e, dict)) if g]
        except Exception as exc:
            logger.warning("peer_groups.json unreadable (%s); using built-in seed", exc)
            groups = list(SEED_GROUPS)
        _groups_cache = (mtime, groups)
        return list(groups)


def save_groups(groups: list[dict[str, Any]]) -> None:
    global _groups_cache
    with _groups_lock:
        _write_groups_file(groups)
        _groups_cache = (os.path.getmtime(GROUPS_FILE), list(groups))


def groups_by_id() -> dict[str, dict[str, Any]]:
    return {g["id"]: g for g in load_groups()}


class DivergenceScanRequest(BaseModel):
    groups: list[str] = Field(default_factory=list, description="Peer group ids to scan")
    symbols: list[str] = Field(default_factory=list, description="Custom set of stocks or ETFs")
    benchmark: str | None = Field(default=None, description="Optional benchmark for the custom set")
    windows: list[int] = Field(default_factory=lambda: list(DEFAULT_WINDOWS))
    z_threshold: float = Field(default=2.0, ge=0.5, le=6.0)
    min_corr: float = Field(default=0.30, ge=0.0, le=0.95)
    custom_label: str = Field(default="Custom Set", max_length=60)


class GroupUpsertRequest(BaseModel):
    id: str | None = Field(default=None, max_length=48)
    name: str = Field(min_length=1, max_length=80)
    benchmark: str | None = Field(default=None, max_length=12)
    category: str = Field(default="Custom", max_length=40)
    members: list[str] = Field(min_length=3, max_length=40)
    notes: str = Field(default="", max_length=400)


def _fetch_history(symbol: str) -> pd.DataFrame:
    # Lazy import to avoid a circular import (main imports this module's router).
    from . import main as main_module

    return main_module.fetch_history(symbol, "6M", ttl=HISTORY_TTL_SEC)


def _validate_symbols(symbols: list[str]) -> list[str]:
    from . import main as main_module

    out: list[str] = []
    for raw in symbols:
        sym = str(raw or "").upper().strip()
        if not sym:
            continue
        if not main_module.SYMBOL_PATTERN.match(sym):
            raise HTTPException(status_code=400, detail=f"Invalid symbol: {raw}")
        if sym not in out:
            out.append(sym)
    return out


def _load_closes(symbols: list[str]) -> tuple[pd.DataFrame, dict[str, str]]:
    """Fetch daily closes for all symbols in parallel. Returns (close_df, errors)."""
    from . import main as main_module

    closes: dict[str, pd.Series] = {}
    errors: dict[str, str] = {}
    workers = max(2, min(6, main_module.SCREENER_HISTORY_WORKERS))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_fetch_history, sym): sym for sym in symbols}
        for future in as_completed(futures):
            sym = futures[future]
            try:
                df = future.result()
                series = df["close"].astype(float)
                series.index = pd.to_datetime(series.index).normalize()
                series = series[~series.index.duplicated(keep="last")]
                closes[sym] = series
            except HTTPException as exc:
                errors[sym] = str(exc.detail)
            except Exception as exc:
                errors[sym] = str(exc)

    if not closes:
        return pd.DataFrame(), errors
    close_df = pd.DataFrame(closes).sort_index()
    # Keep rows where at least 60% of members traded; then forward-fill short gaps
    # (mixed exchanges/ETFs occasionally miss a session).
    min_members = max(2, int(math.ceil(close_df.shape[1] * 0.6)))
    close_df = close_df.dropna(thresh=min_members)
    close_df = close_df.ffill(limit=2)
    return close_df, errors


def _window_return(series: pd.Series, window: int) -> float | None:
    clean = series.dropna()
    if len(clean) < window + 1:
        return None
    last = float(clean.iloc[-1])
    base = float(clean.iloc[-1 - window])
    if base == 0:
        return None
    return (last / base - 1.0) * 100.0


def _robust_z(value: float, peers: list[float]) -> float | None:
    """Robust z-score of value against peer returns (value excluded by caller)."""
    if len(peers) < 3:
        return None
    s = pd.Series(peers, dtype=float)
    median = float(s.median())
    mad = float((s - median).abs().median())
    scale = 1.4826 * mad
    if scale < 1e-6:
        # Degenerate (peers all identical) — fall back to std.
        std = float(s.std(ddof=0))
        if std < 1e-6:
            return None
        scale = std
    z = (value - median) / scale
    return max(-15.0, min(15.0, z))


def _flat_threshold_pct(window: int) -> float:
    # What counts as the group "actually moving" scales with the window length.
    return 0.5 * math.sqrt(window)


def _direction_label(group_ret: float, z: float, window: int) -> str:
    flat = _flat_threshold_pct(window)
    if group_ret <= -flat:
        return "Held up while group fell" if z > 0 else "Fell harder than group"
    if group_ret >= flat:
        return "Led group rally" if z > 0 else "Lagged group rally"
    return "Moved independently of flat group"


def _scan_one_set(
    set_id: str,
    set_name: str,
    members: list[str],
    benchmark: str | None,
    windows: list[int],
    z_threshold: float,
    min_corr: float,
) -> dict[str, Any]:
    from . import main as main_module

    fetch_symbols = list(members)
    if benchmark and benchmark not in fetch_symbols:
        fetch_symbols.append(benchmark)

    close_df, fetch_errors = _load_closes(fetch_symbols)
    member_cols = [m for m in members if m in close_df.columns]
    if len(member_cols) < 3:
        return {
            "id": set_id,
            "name": set_name,
            "benchmark": benchmark,
            "error": "Not enough members with price history (need at least 3).",
            "fetch_errors": fetch_errors,
            "rows": [],
            "windows": {},
        }

    daily_rets = close_df[member_cols].pct_change()
    corr_rets = daily_rets.tail(CORR_LOOKBACK_DAYS)

    window_rets: dict[str, dict[int, float | None]] = {
        sym: {w: _window_return(close_df[sym], w) for w in windows} for sym in member_cols
    }
    benchmark_rets: dict[int, float | None] = {}
    if benchmark and benchmark in close_df.columns:
        benchmark_rets = {w: _window_return(close_df[benchmark], w) for w in windows}

    group_stats: dict[int, dict[str, float | None]] = {}
    for w in windows:
        values = [r for sym in member_cols if (r := window_rets[sym].get(w)) is not None]
        if len(values) >= 3:
            s = pd.Series(values, dtype=float)
            group_stats[w] = {
                "median_ret_pct": float(s.median()),
                "mean_ret_pct": float(s.mean()),
                "benchmark_ret_pct": benchmark_rets.get(w),
            }
        else:
            group_stats[w] = {"median_ret_pct": None, "mean_ret_pct": None, "benchmark_ret_pct": benchmark_rets.get(w)}

    rows: list[dict[str, Any]] = []
    for sym in member_cols:
        peer_cols = [c for c in member_cols if c != sym]
        # Equal-weight peer index (excluding self) for correlation/beta.
        peer_index = corr_rets[peer_cols].mean(axis=1)
        own = corr_rets[sym]
        aligned = pd.concat([own, peer_index], axis=1, keys=["own", "peer"]).dropna()
        corr: float | None = None
        beta: float | None = None
        if len(aligned) >= 20:
            corr_val = float(aligned["own"].corr(aligned["peer"]))
            if not math.isnan(corr_val):
                corr = round(corr_val, 3)
            peer_var = float(aligned["peer"].var())
            if peer_var > 1e-12:
                beta_val = float(aligned["own"].cov(aligned["peer"])) / peer_var
                if not math.isnan(beta_val):
                    beta = round(beta_val, 2)

        z_by_window: dict[int, float | None] = {}
        signals: list[dict[str, Any]] = []
        for w in windows:
            own_ret = window_rets[sym].get(w)
            peers = [r for c in peer_cols if (r := window_rets[c].get(w)) is not None]
            if own_ret is None:
                z_by_window[w] = None
                continue
            z = _robust_z(own_ret, peers)
            z_by_window[w] = round(z, 2) if z is not None else None
            group_ret = group_stats[w]["median_ret_pct"]
            if z is not None and group_ret is not None and abs(z) >= z_threshold:
                signals.append(
                    {
                        "window": w,
                        "z": round(z, 2),
                        "ret_pct": round(own_ret, 2),
                        "group_ret_pct": round(group_ret, 2),
                        "label": _direction_label(group_ret, z, w),
                        "confirmed": bool(corr is not None and corr >= min_corr),
                    }
                )

        clean_close = close_df[sym].dropna()
        z_values = [abs(z) for z in z_by_window.values() if z is not None]
        rows.append(
            {
                "symbol": sym,
                "last_close": round(float(clean_close.iloc[-1]), 4) if len(clean_close) else None,
                "last_date": clean_close.index[-1].date().isoformat() if len(clean_close) else None,
                "returns_pct": {str(w): (round(r, 2) if (r := window_rets[sym].get(w)) is not None else None) for w in windows},
                "z_scores": {str(w): z_by_window.get(w) for w in windows},
                "peer_corr_60d": corr,
                "peer_beta_60d": beta,
                "max_abs_z": round(max(z_values), 2) if z_values else None,
                "signals": signals,
                "is_diverger": bool(any(s["confirmed"] for s in signals)),
                "low_correlation": bool(corr is not None and corr < min_corr),
            }
        )

    rows.sort(key=lambda r: (r["max_abs_z"] is not None, r["max_abs_z"] or 0.0), reverse=True)
    divergers = [r["symbol"] for r in rows if r["is_diverger"]]

    return {
        "id": set_id,
        "name": set_name,
        "benchmark": benchmark,
        "as_of": close_df.index[-1].date().isoformat() if len(close_df.index) else None,
        "members_requested": len(members),
        "members_scanned": len(member_cols),
        "fetch_errors": fetch_errors,
        "windows": {str(w): group_stats[w] for w in windows},
        "rows": rows,
        "divergers": divergers,
        "data_provider": main_module.MARKET_DATA_PROVIDER,
    }


@router.get("/api/divergence/groups")
def divergence_groups() -> dict[str, Any]:
    from . import main as main_module

    groups = load_groups()
    return {
        "updated_at": main_module.now_iso(),
        "groups": [
            {
                "id": g["id"],
                "name": g["name"],
                "category": g.get("category") or "Custom",
                "benchmark": g.get("benchmark"),
                "members": g["members"],
                "member_count": len(g["members"]),
                "notes": g.get("notes") or "",
            }
            for g in groups
        ],
        "category_order": CATEGORY_ORDER,
        "groups_file": GROUPS_FILE,
        "defaults": {
            "windows": DEFAULT_WINDOWS,
            "z_threshold": 2.0,
            "min_corr": 0.30,
            "max_symbols_per_scan": MAX_SYMBOLS_PER_SCAN,
        },
    }


@router.post("/api/divergence/groups")
def divergence_group_create(req: GroupUpsertRequest) -> dict[str, Any]:
    members = _validate_symbols(req.members)
    if len(members) < 3:
        raise HTTPException(status_code=400, detail="A group needs at least 3 valid members.")
    benchmark = _validate_symbols([req.benchmark])[0] if req.benchmark else None
    group = _normalize_group(
        {
            "id": req.id or req.name,
            "name": req.name,
            "benchmark": benchmark,
            "category": req.category,
            "members": members,
            "notes": req.notes,
        }
    )
    if not group:
        raise HTTPException(status_code=400, detail="Invalid group payload.")
    groups = load_groups()
    if any(g["id"] == group["id"] for g in groups):
        raise HTTPException(status_code=409, detail=f"Group id '{group['id']}' already exists.")
    groups.append(group)
    save_groups(groups)
    return {"ok": True, "group": group}


@router.put("/api/divergence/groups/{group_id}")
def divergence_group_update(group_id: str, req: GroupUpsertRequest) -> dict[str, Any]:
    members = _validate_symbols(req.members)
    if len(members) < 3:
        raise HTTPException(status_code=400, detail="A group needs at least 3 valid members.")
    benchmark = _validate_symbols([req.benchmark])[0] if req.benchmark else None
    groups = load_groups()
    idx = next((i for i, g in enumerate(groups) if g["id"] == group_id), None)
    if idx is None:
        raise HTTPException(status_code=404, detail=f"Unknown peer group: {group_id}")
    updated = _normalize_group(
        {
            "id": group_id,
            "name": req.name,
            "benchmark": benchmark,
            "category": req.category,
            "members": members,
            "notes": req.notes,
        }
    )
    if not updated:
        raise HTTPException(status_code=400, detail="Invalid group payload.")
    groups[idx] = updated
    save_groups(groups)
    return {"ok": True, "group": updated}


@router.delete("/api/divergence/groups/{group_id}")
def divergence_group_delete(group_id: str) -> dict[str, Any]:
    groups = load_groups()
    remaining = [g for g in groups if g["id"] != group_id]
    if len(remaining) == len(groups):
        raise HTTPException(status_code=404, detail=f"Unknown peer group: {group_id}")
    save_groups(remaining)
    return {"ok": True, "deleted": group_id, "remaining": len(remaining)}


OVERVIEW_WINDOWS = [1, 5, 21]  # ~1 day, ~1 week, ~1 month of trading days


@router.get("/api/divergence/overview")
def divergence_overview(
    groups: str | None = Query(default=None, description="Comma-separated group ids; default all"),
    windows: str | None = Query(default=None, description="Comma-separated trading-day windows, default 1,5,21"),
) -> dict[str, Any]:
    """Multi-window pulse across all peer groups, computed from daily history.

    Uses the same 6M history (and 30-min cache) as the full scans, so numbers
    are consistent across the page and a pulse warms the cache for any
    follow-up scan. Cold cost is one history fetch per unique symbol; warm
    pulses are free.
    """
    from . import main as main_module

    win_list = sorted({int(w) for w in (windows or "").split(",") if w.strip().isdigit() and 1 <= int(w) <= 120}) or list(
        OVERVIEW_WINDOWS
    )

    all_groups = load_groups()
    if groups:
        wanted = {s.strip().lower() for s in groups.split(",") if s.strip()}
        all_groups = [g for g in all_groups if g["id"] in wanted]
    if not all_groups:
        raise HTTPException(status_code=400, detail="No matching peer groups.")

    symbols: list[str] = []
    for g in all_groups:
        for sym in g["members"]:
            if sym not in symbols:
                symbols.append(sym)
        bench = g.get("benchmark")
        if bench and bench not in symbols:
            symbols.append(bench)

    close_df, fetch_errors = _load_closes(symbols)
    if close_df.empty:
        raise HTTPException(status_code=502, detail="No price history available for overview.")

    ret_map: dict[str, dict[int, float | None]] = {
        sym: {w: _window_return(close_df[sym], w) for w in win_list} for sym in close_df.columns
    }

    rows: list[dict[str, Any]] = []
    for g in all_groups:
        bench = g.get("benchmark")
        member_syms = [s for s in g["members"] if s in ret_map]
        window_stats: dict[str, dict[str, Any]] = {}
        for w in win_list:
            vals = [(sym, r) for sym in member_syms if (r := ret_map[sym].get(w)) is not None]
            if vals:
                pcts = [v for _, v in vals]
                best = max(vals, key=lambda x: x[1])
                worst = min(vals, key=lambda x: x[1])
                window_stats[str(w)] = {
                    "median_pct": round(stat_median(pcts), 2),
                    "mean_pct": round(sum(pcts) / len(pcts), 2),
                    "benchmark_pct": (round(b, 2) if bench and (b := ret_map.get(bench, {}).get(w)) is not None else None),
                    "best": {"symbol": best[0], "pct": round(best[1], 2)},
                    "worst": {"symbol": worst[0], "pct": round(worst[1], 2)},
                }
            else:
                window_stats[str(w)] = {
                    "median_pct": None,
                    "mean_pct": None,
                    "benchmark_pct": (round(b, 2) if bench and (b := ret_map.get(bench, {}).get(w)) is not None else None),
                    "best": None,
                    "worst": None,
                }
        rows.append(
            {
                "id": g["id"],
                "name": g["name"],
                "category": g.get("category") or "Custom",
                "benchmark": bench,
                "members_total": len(g["members"]),
                "members_quoted": len(member_syms),
                "windows": window_stats,
            }
        )

    primary = str(win_list[0])
    rows.sort(
        key=lambda r: (r["windows"][primary]["median_pct"] is not None, r["windows"][primary]["median_pct"] or 0.0),
        reverse=True,
    )
    return {
        "updated_at": main_module.now_iso(),
        "as_of": close_df.index[-1].date().isoformat() if len(close_df.index) else None,
        "windows": win_list,
        "symbols_loaded": int(close_df.shape[1]),
        "symbols_failed": fetch_errors,
        "rows": rows,
    }


@router.post("/api/divergence/scan")
def divergence_scan(req: DivergenceScanRequest) -> dict[str, Any]:
    from . import main as main_module

    windows = sorted({w for w in req.windows if 1 <= int(w) <= 120}) or list(DEFAULT_WINDOWS)
    by_id = groups_by_id()

    sets_to_scan: list[tuple[str, str, list[str], str | None]] = []
    for group_id in req.groups:
        group = by_id.get(str(group_id).strip().lower())
        if not group:
            raise HTTPException(status_code=400, detail=f"Unknown peer group: {group_id}")
        sets_to_scan.append((group["id"], group["name"], list(group["members"]), group.get("benchmark")))

    custom_symbols = _validate_symbols(req.symbols)
    if custom_symbols:
        if len(custom_symbols) < 3:
            raise HTTPException(status_code=400, detail="Custom set needs at least 3 symbols to compare against each other.")
        benchmark = _validate_symbols([req.benchmark])[0] if req.benchmark else None
        sets_to_scan.append(("custom", req.custom_label or "Custom Set", custom_symbols, benchmark))

    if not sets_to_scan:
        raise HTTPException(status_code=400, detail="Select at least one peer group or provide a custom symbol set.")

    total_symbols = sum(len(members) + (1 if bench else 0) for _, _, members, bench in sets_to_scan)
    if total_symbols > MAX_SYMBOLS_PER_SCAN:
        raise HTTPException(
            status_code=400,
            detail=f"Scan covers {total_symbols} symbols; max per scan is {MAX_SYMBOLS_PER_SCAN}. Select fewer groups.",
        )

    results = [
        _scan_one_set(set_id, set_name, members, bench, windows, req.z_threshold, req.min_corr)
        for set_id, set_name, members, bench in sets_to_scan
    ]

    all_divergers = [
        {"group": r["name"], "symbol": sym}
        for r in results
        for sym in r.get("divergers", [])
    ]
    return {
        "updated_at": main_module.now_iso(),
        "params": {
            "windows": windows,
            "z_threshold": req.z_threshold,
            "min_corr": req.min_corr,
        },
        "results": results,
        "diverger_count": len(all_divergers),
        "divergers": all_divergers,
    }
