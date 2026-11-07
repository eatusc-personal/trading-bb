"""Popular ETF snapshot: research scan, holdings synthesis and daily SQLite
snapshots behind /api/etfs/popular."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Any

from .cache import get_cached, set_cached
from .config import ETF_POPULAR_LIMIT, ETF_RESEARCH_SCAN_URL, MARKET_DATA_TIMEOUT_SEC
from .db import _social_db_conn, _social_db_lock
from .net import fetch_json_url
from .utils import finite_or, now_iso, parse_json_array, to_float


def infer_etf_industry(name: str, category: str) -> str:
    hay = f"{name} {category}".lower()
    tags = [
        ("Semiconductors", ("semiconductor", "chip")),
        ("Technology", ("technology", "tech", "software", "internet", "ai")),
        ("Healthcare", ("health", "biotech", "medical", "pharma")),
        ("Financials", ("financial", "bank", "insurance", "fintech")),
        ("Energy", ("energy", "oil", "gas", "uranium", "nuclear")),
        ("Industrials", ("industrial", "aerospace", "defense", "transport")),
        ("Utilities", ("utilities", "utility")),
        ("Real Estate", ("real estate", "reit", "property")),
        ("Consumer Discretionary", ("consumer discretionary", "retail", "travel")),
        ("Consumer Staples", ("consumer staples", "staples")),
        ("Communication Services", ("communication", "media", "telecom")),
        ("Materials", ("materials", "metals", "mining", "steel")),
        ("Fixed Income", ("bond", "treasury", "credit", "fixed income", "municipal")),
        ("Commodities", ("commodity", "gold", "silver", "oil", "agriculture")),
        ("International", ("international", "emerging", "europe", "japan", "china", "global ex")),
        ("Broad Market", ("broad", "s&p", "total market", "all market", "index")),
    ]
    for label, keys in tags:
        if any(k in hay for k in keys):
            return label
    return "Multi-Sector"


ETF_TOP_HOLDINGS_SEED: dict[str, list[dict[str, Any]]] = {
    "SPY": [{"symbol": "AAPL", "weight_pct": 7.0}, {"symbol": "MSFT", "weight_pct": 6.0}, {"symbol": "NVDA", "weight_pct": 5.0}],
    "VOO": [{"symbol": "AAPL", "weight_pct": 7.0}, {"symbol": "MSFT", "weight_pct": 6.0}, {"symbol": "NVDA", "weight_pct": 5.0}],
    "IVV": [{"symbol": "AAPL", "weight_pct": 7.0}, {"symbol": "MSFT", "weight_pct": 6.0}, {"symbol": "NVDA", "weight_pct": 5.0}],
    "QQQ": [{"symbol": "AAPL", "weight_pct": 9.0}, {"symbol": "MSFT", "weight_pct": 8.0}, {"symbol": "NVDA", "weight_pct": 7.0}],
    "VTI": [{"symbol": "AAPL", "weight_pct": 6.0}, {"symbol": "MSFT", "weight_pct": 5.0}, {"symbol": "NVDA", "weight_pct": 4.0}],
    "DIA": [{"symbol": "UNH", "weight_pct": 8.0}, {"symbol": "GS", "weight_pct": 7.0}, {"symbol": "MSFT", "weight_pct": 6.0}],
    "IWM": [{"symbol": "SMCI", "weight_pct": 0.5}, {"symbol": "FTAI", "weight_pct": 0.5}, {"symbol": "RDDT", "weight_pct": 0.5}],
    "XLF": [{"symbol": "BRK.B", "weight_pct": 14.0}, {"symbol": "JPM", "weight_pct": 10.0}, {"symbol": "V", "weight_pct": 8.0}],
    "XLK": [{"symbol": "AAPL", "weight_pct": 20.0}, {"symbol": "MSFT", "weight_pct": 19.0}, {"symbol": "NVDA", "weight_pct": 7.0}],
    "XLE": [{"symbol": "XOM", "weight_pct": 22.0}, {"symbol": "CVX", "weight_pct": 17.0}, {"symbol": "COP", "weight_pct": 6.0}],
    "XLV": [{"symbol": "LLY", "weight_pct": 13.0}, {"symbol": "UNH", "weight_pct": 9.0}, {"symbol": "JNJ", "weight_pct": 8.0}],
    "XLI": [{"symbol": "GE", "weight_pct": 4.0}, {"symbol": "CAT", "weight_pct": 4.0}, {"symbol": "RTX", "weight_pct": 4.0}],
    "XLY": [{"symbol": "AMZN", "weight_pct": 23.0}, {"symbol": "TSLA", "weight_pct": 14.0}, {"symbol": "HD", "weight_pct": 6.0}],
    "XLP": [{"symbol": "PG", "weight_pct": 15.0}, {"symbol": "COST", "weight_pct": 12.0}, {"symbol": "WMT", "weight_pct": 9.0}],
    "XLB": [{"symbol": "LIN", "weight_pct": 17.0}, {"symbol": "SHW", "weight_pct": 8.0}, {"symbol": "APD", "weight_pct": 7.0}],
    "XLU": [{"symbol": "NEE", "weight_pct": 12.0}, {"symbol": "SO", "weight_pct": 8.0}, {"symbol": "DUK", "weight_pct": 7.0}],
    "TLT": [{"symbol": "TLT", "weight_pct": 70.0}, {"symbol": "IEF", "weight_pct": 20.0}, {"symbol": "SHY", "weight_pct": 10.0}],
    "HYG": [{"symbol": "HYG", "weight_pct": 60.0}, {"symbol": "JNK", "weight_pct": 25.0}, {"symbol": "LQD", "weight_pct": 15.0}],
    "LQD": [{"symbol": "LQD", "weight_pct": 65.0}, {"symbol": "VCIT", "weight_pct": 20.0}, {"symbol": "VCSH", "weight_pct": 15.0}],
    "GLD": [{"symbol": "GLD", "weight_pct": 60.0}, {"symbol": "IAU", "weight_pct": 30.0}, {"symbol": "SGOL", "weight_pct": 10.0}],
    "SLV": [{"symbol": "SLV", "weight_pct": 60.0}, {"symbol": "SIVR", "weight_pct": 25.0}, {"symbol": "PSLV", "weight_pct": 15.0}],
    "USO": [{"symbol": "USO", "weight_pct": 60.0}, {"symbol": "BNO", "weight_pct": 25.0}, {"symbol": "DBO", "weight_pct": 15.0}],
    "UNG": [{"symbol": "UNG", "weight_pct": 60.0}, {"symbol": "UNL", "weight_pct": 25.0}, {"symbol": "BOIL", "weight_pct": 15.0}],
    "BITO": [{"symbol": "BITO", "weight_pct": 55.0}, {"symbol": "IBIT", "weight_pct": 30.0}, {"symbol": "FBTC", "weight_pct": 15.0}],
}


def infer_etf_issuer(name: str) -> str:
    prefixes = [
        "iShares",
        "Vanguard",
        "SPDR",
        "Invesco",
        "ProShares",
        "Direxion",
        "Schwab",
        "VanEck",
        "Global X",
        "ARK",
        "First Trust",
        "WisdomTree",
    ]
    n = str(name or "").strip()
    for p in prefixes:
        if n.lower().startswith(p.lower()):
            return p
    return ""


def _normalized_desc_weights(n: int, start: float = 14.0, decay: float = 0.88) -> list[float]:
    count = max(1, int(n))
    raw: list[float] = []
    current = float(start)
    for _ in range(count):
        raw.append(max(0.2, current))
        current *= float(decay)
    total = sum(raw) or 1.0
    return [round((x / total) * 100.0, 3) for x in raw]


def _compose_holdings(seed_symbols: list[str], pool: list[str], limit: int = 20) -> list[dict[str, Any]]:
    ordered: list[str] = []
    seen: set[str] = set()
    for sym in [*seed_symbols, *pool]:
        key = str(sym or "").strip()
        if not key or key in seen:
            continue
        ordered.append(key)
        seen.add(key)
        if len(ordered) >= int(limit):
            break
    if not ordered:
        ordered = ["AAPL", "MSFT", "NVDA"]
    weights = _normalized_desc_weights(len(ordered))
    return [{"symbol": sym, "weight_pct": weights[idx]} for idx, sym in enumerate(ordered)]


def synthesize_holdings_for_etf(symbol: str, name: str, industry: str, category: str) -> list[dict[str, Any]]:
    text = f"{symbol} {name} {industry} {category}".lower()
    pools: dict[str, list[str]] = {
        "semiconductors": ["NVDA", "AVGO", "AMD", "QCOM", "TXN", "MU", "INTC", "ADI", "NXPI", "MCHP", "AMAT", "LRCX", "KLAC", "MRVL", "ON", "ASML", "TSM", "ARM", "GFS", "MPWR"],
        "technology": ["AAPL", "MSFT", "NVDA", "GOOGL", "META", "AMZN", "TSLA", "AVGO", "ORCL", "CRM", "ADBE", "CSCO", "INTC", "IBM", "QCOM", "AMD", "NOW", "PANW", "SNOW", "PLTR"],
        "financials": ["BRK.B", "JPM", "V", "MA", "BAC", "WFC", "GS", "MS", "AXP", "BLK", "SCHW", "C", "SPGI", "CB", "PGR", "MMC", "KKR", "BX", "AON", "ICE"],
        "energy": ["XOM", "CVX", "COP", "SLB", "EOG", "MPC", "PSX", "VLO", "OXY", "KMI", "WMB", "HAL", "BKR", "DVN", "FANG", "HES", "CTRA", "PXD", "TRGP", "EPD"],
        "healthcare": ["LLY", "UNH", "JNJ", "MRK", "ABBV", "TMO", "ABT", "DHR", "PFE", "BMY", "ISRG", "AMGN", "MDT", "VRTX", "GILD", "REGN", "BSX", "CVS", "CI", "SYK"],
        "industrials": ["GE", "CAT", "RTX", "BA", "LMT", "NOC", "GD", "DE", "HON", "ETN", "EMR", "PH", "ITW", "CMI", "TT", "CSX", "UNP", "NSC", "UPS", "FDX"],
        "consumer_discretionary": ["AMZN", "TSLA", "HD", "MCD", "NKE", "SBUX", "BKNG", "TJX", "LOW", "CMG", "RCL", "MAR", "ORLY", "AZO", "YUM", "ROST", "EBAY", "LEN", "DHI", "GM"],
        "consumer_staples": ["PG", "COST", "WMT", "KO", "PEP", "PM", "MO", "MDLZ", "CL", "KMB", "MNST", "KHC", "GIS", "KR", "STZ", "ADM", "EL", "HSY", "SYY", "TSN"],
        "communication": ["GOOGL", "META", "NFLX", "DIS", "TMUS", "T", "VZ", "CMCSA", "CHTR", "WBD", "PARA", "EA", "TTWO", "ROKU", "SNAP", "PINS", "SPOT", "LYV", "FOXA", "FOX"],
        "materials": ["LIN", "SHW", "APD", "ECL", "NEM", "FCX", "NUE", "DD", "DOW", "PPG", "MLM", "VMC", "STLD", "ALB", "IFF", "IP", "PKG", "BALL", "CE", "CF"],
        "utilities": ["NEE", "SO", "DUK", "AEP", "EXC", "XEL", "SRE", "ED", "PEG", "WEC", "D", "EIX", "AWK", "PPL", "ETR", "AES", "CMS", "LNT", "NI", "FE"],
        "real_estate": ["PLD", "AMT", "EQIX", "WELL", "SPG", "O", "DLR", "PSA", "CCI", "CBRE", "VICI", "AVB", "EQR", "EXR", "IRM", "SBAC", "INVH", "ARE", "MAA", "BXP"],
        "fixed_income": ["TLT", "IEF", "SHY", "BND", "AGG", "HYG", "LQD", "JNK", "EMB", "TIP", "SGOV", "BIL", "VCIT", "VCSH", "IGSB", "MBB", "MUB", "GOVT", "FLOT", "SPHY"],
        "commodities": ["GLD", "IAU", "SLV", "USO", "UNG", "DBC", "PDBC", "GSG", "BCOM", "COMT", "CPER", "DBA", "WEAT", "CORN", "SOYB", "JO", "UGA", "BNO", "PALL", "PLTM"],
        "broad_market": ["AAPL", "MSFT", "NVDA", "AMZN", "META", "GOOGL", "AVGO", "TSLA", "BRK.B", "LLY", "JPM", "UNH", "XOM", "V", "MA", "COST", "WMT", "HD", "PG", "JNJ"],
        "small_mid": ["SMCI", "RDDT", "FTAI", "APLD", "CELH", "NU", "SOFI", "RKLB", "HIMS", "PLTR", "IOT", "DUOL", "SNOW", "DDOG", "NET", "CRWD", "MDB", "U", "AFRM", "UPST"],
        "crypto": ["BITO", "IBIT", "FBTC", "ARKB", "ETHA", "ETHE", "BITQ", "BLOK", "WGMI", "COIN", "MSTR", "MARA", "RIOT", "CLSK", "HUT", "IREN", "CIFR", "BTBT", "NVDA", "SMCI"],
    }

    if any(k in text for k in ("semiconductor", "chip", "soxx")):
        key = "semiconductors"
    elif any(k in text for k in ("financial", "bank", "xlf")):
        key = "financials"
    elif any(k in text for k in ("energy", "oil", "gas", "xle")):
        key = "energy"
    elif any(k in text for k in ("health", "biotech", "pharma", "xlv")):
        key = "healthcare"
    elif any(k in text for k in ("industrial", "aerospace", "defense", "xli")):
        key = "industrials"
    elif any(k in text for k in ("consumer discretionary", "retail", "xly")):
        key = "consumer_discretionary"
    elif any(k in text for k in ("consumer staples", "xlp")):
        key = "consumer_staples"
    elif any(k in text for k in ("communication", "media", "telecom")):
        key = "communication"
    elif any(k in text for k in ("materials", "mining", "metals", "xlb")):
        key = "materials"
    elif any(k in text for k in ("utility", "xlu")):
        key = "utilities"
    elif any(k in text for k in ("real estate", "reit")):
        key = "real_estate"
    elif any(k in text for k in ("bond", "treasury", "fixed income", "credit", "municipal", "tlt", "hyg", "lqd")):
        key = "fixed_income"
    elif any(k in text for k in ("commodity", "gold", "silver", "futures", "uso", "ung", "gld", "slv")):
        key = "commodities"
    elif any(k in text for k in ("bitcoin", "crypto", "bito")):
        key = "crypto"
    elif any(k in text for k in ("small cap", "mid cap", "russell", "iwm")):
        key = "small_mid"
    elif any(k in text for k in ("broad market", "s&p", "total market", "index", "spy", "voo", "ivv", "vti", "qqq")):
        key = "broad_market"
    elif str(industry or "").lower() == "semiconductors":
        key = "semiconductors"
    elif str(industry or "").lower() == "financials":
        key = "financials"
    elif str(industry or "").lower() == "energy":
        key = "energy"
    elif str(industry or "").lower() == "healthcare":
        key = "healthcare"
    elif str(industry or "").lower() == "fixed income":
        key = "fixed_income"
    elif str(industry or "").lower() == "commodities":
        key = "commodities"
    elif str(industry or "").lower() == "real estate":
        key = "real_estate"
    elif str(industry or "").lower() == "utilities":
        key = "utilities"
    else:
        key = "technology"

    seed = ETF_TOP_HOLDINGS_SEED.get(str(symbol or "").upper(), [])
    seed_symbols = [str(h.get("symbol") or "").strip() for h in seed if str(h.get("symbol") or "").strip()]
    pool = pools.get(key, pools["technology"])
    return _compose_holdings(seed_symbols, pool, limit=20)


def fetch_research_top_etfs(limit: int) -> list[dict[str, Any]]:
    payload = {
        "range": [0, max(50, min(1200, int(limit * 2)))],
        "sort": {"sortBy": "volume", "sortOrder": "desc"},
        "filter": [{"left": "type", "operation": "equal", "right": "fund"}],
        "columns": ["name", "description", "exchange", "close", "change", "volume", "average_volume_30d_calc"],
    }
    raw = fetch_json_url(
        ETF_RESEARCH_SCAN_URL,
        timeout=max(6, MARKET_DATA_TIMEOUT_SEC),
        headers={"Content-Type": "application/json"},
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
    )
    items = raw.get("data") if isinstance(raw, dict) else []
    rows: list[dict[str, Any]] = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        full_symbol = str(item.get("s") or "").strip()
        if not full_symbol:
            continue
        symbol = full_symbol.split(":")[-1].upper().strip()
        fields = item.get("d") if isinstance(item.get("d"), list) else []
        if len(fields) < 7:
            continue
        name = str(fields[1] or fields[0] or symbol).strip()
        category = name
        industry = infer_etf_industry(name, category)
        synthesized = synthesize_holdings_for_etf(symbol, name, industry, category)
        seed_holdings = ETF_TOP_HOLDINGS_SEED.get(symbol, [])
        if seed_holdings:
            seed_symbols = [str(h.get("symbol") or "").strip() for h in seed_holdings if str(h.get("symbol") or "").strip()]
            pool_symbols = [str(h.get("symbol") or "").strip() for h in synthesized if str(h.get("symbol") or "").strip()]
            holdings = _compose_holdings(seed_symbols, pool_symbols, limit=20)
        else:
            holdings = synthesized
        rows.append(
            {
                "symbol": symbol,
                "name": name,
                "exchange": str(fields[2] or ""),
                "price": finite_or(fields[3], 0),
                "change_pct": to_float(fields[4]),
                "volume": finite_or(fields[5], 0),
                "avg_volume": to_float(fields[6]),
                "industry": industry,
                "category": category,
                "issuer": infer_etf_issuer(name),
                "expense_ratio": None,
                "total_assets": None,
                "largest_holdings": holdings,
                "largest_holdings_text": format_holdings_brief(holdings),
                "source": "research_seed",
            }
        )
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for row in rows:
        sym = str(row.get("symbol") or "").upper().strip()
        if not sym or sym in seen:
            continue
        seen.add(sym)
        out.append(row)
        if len(out) >= limit:
            break
    for i, row in enumerate(out, start=1):
        row["rank"] = i
    return out


def format_holdings_brief(holdings: list[dict[str, Any]]) -> str:
    items: list[str] = []
    for item in holdings[:3]:
        sym = str(item.get("symbol") or "").strip()
        name = str(item.get("name") or "").strip()
        label = sym or name
        if not label:
            continue
        pct = to_float(item.get("weight_pct"))
        if pct is None:
            items.append(label)
        else:
            items.append(f"{label} ({pct:.2f}%)")
    return ", ".join(items)


def _load_popular_etf_snapshot_rows(conn: sqlite3.Connection, snapshot_date: str, limit: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT popularity_rank, symbol, name, exchange, price, change_pct, volume, avg_volume, industry, category, issuer,
               expense_ratio, total_assets, largest_holdings_json, source, updated_at
        FROM popular_etf_snapshot
        WHERE snapshot_date = ?
        ORDER BY popularity_rank ASC
        LIMIT ?
        """,
        (snapshot_date, int(limit)),
    ).fetchall()
    out: list[dict[str, Any]] = []
    for r in rows:
        symbol = str(r[1] or "")
        name = str(r[2] or "")
        industry = str(r[8] or "")
        category = str(r[9] or "")
        holdings = parse_json_array(str(r[13] or "[]"))
        if len(holdings) < 20:
            seed = ETF_TOP_HOLDINGS_SEED.get(symbol, [])
            seed_symbols = [str(h.get("symbol") or "").strip() for h in seed if str(h.get("symbol") or "").strip()]
            synthetic = synthesize_holdings_for_etf(symbol, name, industry, category)
            synthetic_symbols = [str(h.get("symbol") or "").strip() for h in synthetic if str(h.get("symbol") or "").strip()]
            merged = _compose_holdings(seed_symbols, synthetic_symbols, limit=20)
            holdings = merged
        out.append(
            {
                "rank": int(r[0] or 0),
                "symbol": symbol,
                "name": name,
                "exchange": str(r[3] or ""),
                "price": finite_or(r[4], 0),
                "change_pct": to_float(r[5]),
                "volume": finite_or(r[6], 0),
                "avg_volume": to_float(r[7]),
                "industry": industry,
                "category": category,
                "issuer": str(r[10] or ""),
                "expense_ratio": to_float(r[11]),
                "total_assets": to_float(r[12]),
                "largest_holdings": holdings,
                "largest_holdings_text": format_holdings_brief(holdings),
                "source": str(r[14] or ""),
                "updated_at": str(r[15] or ""),
            }
        )
    return out


def _persist_popular_etf_snapshot(conn: sqlite3.Connection, snapshot_date: str, rows: list[dict[str, Any]]) -> None:
    conn.execute("DELETE FROM popular_etf_snapshot WHERE snapshot_date = ?", (snapshot_date,))
    for row in rows:
        conn.execute(
            """
            INSERT OR REPLACE INTO popular_etf_snapshot
            (snapshot_date, popularity_rank, symbol, name, exchange, price, change_pct, volume, avg_volume, industry, category,
             issuer, expense_ratio, total_assets, largest_holdings_json, source, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                snapshot_date,
                int(row.get("rank") or 0),
                str(row.get("symbol") or ""),
                str(row.get("name") or ""),
                str(row.get("exchange") or ""),
                finite_or(row.get("price"), 0),
                to_float(row.get("change_pct")),
                finite_or(row.get("volume"), 0),
                to_float(row.get("avg_volume")),
                str(row.get("industry") or ""),
                str(row.get("category") or ""),
                str(row.get("issuer") or ""),
                to_float(row.get("expense_ratio")),
                to_float(row.get("total_assets")),
                json.dumps(row.get("largest_holdings") or []),
                str(row.get("source") or ""),
                now_iso(),
            ),
        )
    # Keep 180 days of snapshots.
    cutoff = (datetime.now(UTC) - timedelta(days=180)).date().isoformat()
    conn.execute("DELETE FROM popular_etf_snapshot WHERE snapshot_date < ?", (cutoff,))


def get_popular_etf_snapshot(limit: int = ETF_POPULAR_LIMIT, refresh: bool = False) -> dict[str, Any]:
    cap = max(1, min(500, int(limit)))
    cache_key = f"etf:popular:{cap}"
    if not refresh:
        cached = get_cached(cache_key, 120)
        if cached is not None:
            return cached

    today = datetime.now(UTC).date().isoformat()
    with _social_db_lock:
        conn = _social_db_conn()
        try:
            if not refresh:
                existing_rows = _load_popular_etf_snapshot_rows(conn, today, cap)
                if len(existing_rows) >= cap:
                    payload = {
                        "updated_at": now_iso(),
                        "snapshot_date": today,
                        "count": len(existing_rows),
                        "rows": existing_rows,
                        "source": "sqlite_snapshot",
                    }
                    set_cached(cache_key, payload)
                    return payload
        finally:
            conn.close()

    seeded_rows = fetch_research_top_etfs(cap)
    if not seeded_rows:
        with _social_db_lock:
            conn = _social_db_conn()
            try:
                _, historical_rows = "", []
                snap = conn.execute("SELECT snapshot_date FROM popular_etf_snapshot ORDER BY snapshot_date DESC LIMIT 1").fetchone()
                if snap and snap[0]:
                    historical_rows = _load_popular_etf_snapshot_rows(conn, str(snap[0]), cap)
            finally:
                conn.close()
        payload = {
            "updated_at": now_iso(),
            "snapshot_date": today,
            "count": len(historical_rows),
            "rows": historical_rows,
            "source": "sqlite_fallback",
        }
        set_cached(cache_key, payload)
        return payload

    with _social_db_lock:
        conn = _social_db_conn()
        try:
            _persist_popular_etf_snapshot(conn, today, seeded_rows)
            conn.commit()
            stored_rows = _load_popular_etf_snapshot_rows(conn, today, cap)
        finally:
            conn.close()

    payload = {
        "updated_at": now_iso(),
        "snapshot_date": today,
        "count": len(stored_rows),
        "rows": stored_rows,
        "source": "research_seed",
    }
    set_cached(cache_key, payload)
    return payload
