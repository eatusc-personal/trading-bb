"""Daily cross-tab report: confluence-ranked trading opportunities.

Generalizes every tab's live numbers for the day into one view:

- Pulls the SAME cached results the individual tabs serve (market overview,
  after-hours, alerts, news, market indicators, social sentiment, divergence
  group pulse, popular ETFs) — riding the existing swr_serve keys, so building
  a report is nearly free when the tabs are warm and never re-pings providers.
- Cross-references symbols across sources into a per-symbol signal list, scores
  the confluence (a symbol flagged by several independent sources outranks a
  symbol flagged by one), and emits a ranked opportunity list with
  plain-English reasons.
- Also emits per-tab "daily brief" sections (market pulse, divergence
  highlights, social standouts, technical flags, news, ETF pulse).

GET /api/report/daily?watchlist=AAPL,MSFT&refresh=false

The market-wide report is SWR-cached (key "report:daily"). The watchlist
overlay is computed per request from the cached report's symbol-signal map plus
a (60s-TTL cached) quote fetch, so it stays personal without fragmenting the
report cache.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from fastapi import APIRouter, Query

logger = logging.getLogger("openbb_local_api.report")

router = APIRouter()

REPORT_REFRESH_AFTER_SEC = 180
MAX_OPPORTUNITIES = 15
MAX_WATCHLIST_SYMBOLS = 30

CASHTAG_RE = re.compile(r"\$([A-Z]{1,5})(?![A-Za-z0-9])")

# Buckets whose day-move points get down-weighted so a +400% micro-cap squeeze
# doesn't bury real signal confluence on tradable names.
_SMALL_CAP_BUCKETS = {"micro", "nano"}


def _f(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if out != out or out in (float("inf"), float("-inf")):
        return None
    return out


def _cap_weight(bucket: Any) -> float:
    return 0.5 if str(bucket or "").strip().lower() in _SMALL_CAP_BUCKETS else 1.0


class _Signals:
    """Per-symbol signal accumulator."""

    def __init__(self) -> None:
        self.by_symbol: dict[str, list[dict[str, Any]]] = {}
        self.quote: dict[str, dict[str, Any]] = {}

    def add(self, symbol: Any, source: str, points: float, reason: str, direction: int = 0) -> None:
        sym = str(symbol or "").strip().upper()
        if not sym or points <= 0:
            return
        entries = self.by_symbol.get(sym, [])
        if any(e["source"] == source and e["reason"] == reason for e in entries):
            return  # discovery lists can repeat a symbol — count each signal once
        self.by_symbol.setdefault(sym, []).append(
            {"source": source, "points": round(float(points), 1), "reason": reason, "dir": int(direction)}
        )

    def remember_quote(self, symbol: Any, price: Any, pct: Any) -> None:
        sym = str(symbol or "").strip().upper()
        if not sym:
            return
        entry = self.quote.setdefault(sym, {})
        if entry.get("price") is None and _f(price) is not None:
            entry["price"] = round(_f(price), 4)
        if entry.get("percent_change") is None and _f(pct) is not None:
            entry["percent_change"] = round(_f(pct), 2)


def _collect_market(sig: _Signals, overview: dict[str, Any]) -> None:
    for kind, direction in (("gainers", 1), ("losers", -1)):
        for row in overview.get(kind, []) or []:
            pct = _f(row.get("percent_change"))
            price = _f(row.get("price"))
            if pct is None or (price is not None and price < 0.5):
                continue
            sig.remember_quote(row.get("symbol"), price, pct)
            pts = min(15.0, abs(pct) * 1.2) * _cap_weight(row.get("market_cap_bucket"))
            sig.add(row.get("symbol"), "market", pts, f"{pct:+.1f}% today", direction)
    for row in (overview.get("active") or [])[:15]:
        pct = _f(row.get("percent_change"))
        sig.remember_quote(row.get("symbol"), row.get("price"), pct)
        sig.add(row.get("symbol"), "market", 4.0, "among most active by volume", 0)


def _collect_after_hours(sig: _Signals, payload: dict[str, Any]) -> None:
    rows = sorted(
        (r for r in payload.get("rows", []) or [] if _f(r.get("percent_change")) is not None),
        key=lambda r: abs(_f(r.get("percent_change")) or 0.0),
        reverse=True,
    )
    for row in rows[:10]:
        pct = _f(row.get("percent_change")) or 0.0
        session = "pre-market" if str(row.get("session_type") or "").startswith("pre") else "after-hours"
        pts = min(10.0, abs(pct) * 0.25) * _cap_weight(row.get("market_cap_bucket"))
        bucket = str(row.get("market_cap_bucket") or "").strip()
        suffix = f" ({bucket} cap)" if bucket else ""
        sig.add(
            row.get("symbol"), "after_hours", pts,
            f"{session} {pct:+.1f}%{suffix}", 1 if pct > 0 else -1,
        )


_ALERT_RULES: list[tuple[str, float, int, str | None]] = [
    # (substring lowercase, points, direction, reason override)
    ("rsi < 30", 12.0, 1, "RSI oversold (<30)"),
    ("rsi > 70", 12.0, -1, "RSI overbought (>70)"),
    ("macd bull", 10.0, 1, None),
    ("macd bear", 10.0, -1, None),
    ("bullish", 8.0, 1, None),
    ("bearish", 8.0, -1, None),
    ("volume", 10.0, 0, None),
    ("above 200", 4.0, 1, None),
    ("above 50", 4.0, 1, None),
]


def _collect_alerts(sig: _Signals, payload: dict[str, Any]) -> None:
    for row in payload.get("rows", []) or []:
        sig.remember_quote(row.get("symbol"), row.get("price"), row.get("change_pct"))
        for alert in row.get("alerts", []) or []:
            text = str(alert)
            lowered = text.lower()
            for needle, pts, direction, override in _ALERT_RULES:
                if needle in lowered:
                    sig.add(row.get("symbol"), "technical", pts, override or text, direction)
                    break
            else:
                sig.add(row.get("symbol"), "technical", 6.0, text, 0)


def _collect_social(sig: _Signals, payload: dict[str, Any]) -> None:
    for row in payload.get("rows", []) or []:
        if str(row.get("validation_status") or "") != "passed":
            continue
        tier = str(row.get("candidate_tier") or "").upper()
        if tier not in {"A", "B"}:
            continue
        direction = {"bullish": 1, "bearish": -1}.get(str(row.get("sentiment_label") or ""), 0)
        pts = 15.0 if tier == "A" else 8.0
        bits = [f"social tier {tier}"]
        buzz = _f(row.get("mention_buzz_ratio"))
        if buzz is not None and buzz >= 1.8:
            pts += 8.0 if buzz >= 3 else 4.0
            bits.append(f"buzz {buzz:.1f}x 30d baseline")
        if row.get("cross_source_confirmed"):
            pts += 3.0
            bits.append("multi-source")
        label = str(row.get("sentiment_label") or "")
        if label and label != "neutral":
            bits.append(f"{label} crowd")
        sig.add(row.get("ticker"), "social", pts, ", ".join(bits), direction)


def _collect_divergence(sig: _Signals, payload: dict[str, Any]) -> None:
    for group in payload.get("rows", []) or []:
        stats = (group.get("windows") or {}).get("1") or {}
        median = _f(stats.get("median_pct"))
        best = stats.get("best") or {}
        worst = stats.get("worst") or {}
        if median is None:
            continue
        name = str(group.get("name") or group.get("id") or "peer group")
        best_pct = _f(best.get("pct"))
        if best_pct is not None and best_pct - median >= 3.0:
            verb = "held up while group fell" if median <= -1.0 else "led the group"
            sig.add(
                best.get("symbol"), "divergence", min(12.0, best_pct - median),
                f"{verb}: {best_pct:+.1f}% vs {name} median {median:+.1f}% (1D)", 1,
            )
        worst_pct = _f(worst.get("pct"))
        if worst_pct is not None and median - worst_pct >= 3.0:
            verb = "lagged group rally" if median >= 1.0 else "fell hardest in group"
            sig.add(
                worst.get("symbol"), "divergence", min(12.0, median - worst_pct),
                f"{verb}: {worst_pct:+.1f}% vs {name} median {median:+.1f}% (1D)", -1,
            )


def _news_cashtags(payload: dict[str, Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in payload.get("rows", []) or []:
        for sym in set(CASHTAG_RE.findall(str(row.get("title") or ""))):
            counts[sym] = counts.get(sym, 0) + 1
    return counts


def _collect_news(sig: _Signals, cashtags: dict[str, int]) -> None:
    for sym, count in cashtags.items():
        plural = "s" if count > 1 else ""
        sig.add(sym, "news", min(8.0, 3.0 * count), f"{count} headline{plural} today", 0)


def _collect_earnings(sig: _Signals, indicators: dict[str, Any]) -> None:
    events = (indicators.get("earnings") or {}).get("next_events") or []
    for ev in events:
        if not isinstance(ev, dict):
            continue
        sym = ev.get("symbol") or ev.get("ticker")
        date = ev.get("date") or ev.get("report_date") or ev.get("earnings_date") or "soon"
        sig.add(sym, "catalyst", 5.0, f"earnings {date}", 0)


def _summarize_symbol(entries: list[dict[str, Any]]) -> dict[str, Any]:
    total = sum(e["points"] for e in entries)
    net = sum(e["points"] * e["dir"] for e in entries)
    if net >= 5:
        direction = "bullish"
    elif net <= -5:
        direction = "bearish"
    else:
        direction = "mixed"
    ordered = sorted(entries, key=lambda e: e["points"], reverse=True)
    return {
        "score": int(min(100, round(total))),
        "direction": direction,
        "sources": sorted({e["source"] for e in entries}),
        "reasons": [e["reason"] for e in ordered],
    }


def _build_report() -> dict[str, Any]:
    from . import divergence as divergence_module
    from . import main as main_module

    sources: dict[str, dict[str, Any]] = {}

    def pull(name: str, producer: Any) -> dict[str, Any]:
        try:
            payload = producer()
            if not isinstance(payload, dict):
                raise ValueError("unexpected payload type")
            sources[name] = {"ok": True, "updated_at": payload.get("updated_at")}
            return payload
        except Exception as exc:  # one dead source must not kill the report
            logger.warning("report: source %s failed: %s", name, exc)
            sources[name] = {"ok": False, "error": str(exc)}
            return {}

    overview = pull(
        "market", lambda: main_module.swr_serve("market:overview", main_module._build_market_overview, 60)[0]
    )
    after_hours = pull(
        "after_hours",
        lambda: main_module.swr_serve("market:after_hours", main_module._build_market_after_hours, 120)[0],
    )
    alerts = pull(
        "alerts", lambda: main_module.swr_serve("alerts:default", lambda: main_module._build_alerts(None), 120)[0]
    )
    news = pull(
        "news", lambda: main_module.swr_serve("news:headlines:30", lambda: main_module._build_news_headlines(30), 300)[0]
    )
    indicators = pull(
        "indicators",
        lambda: main_module.swr_serve(
            "market:indicators:default", lambda: main_module._build_market_indicators(None), 300
        )[0],
    )

    def _social_payload() -> dict[str, Any]:
        with main_module._social_result_lock:
            payload = main_module._social_result_state["payload"]
        if payload is None:
            raise RuntimeError("social scan has not completed yet")
        return payload

    social = pull("social", _social_payload)
    divergence = pull("divergence", lambda: divergence_module.divergence_overview(groups=None, windows="1,5"))
    etfs = pull("etfs", lambda: main_module.get_popular_etf_snapshot(limit=10, refresh=False))

    sig = _Signals()
    _collect_market(sig, overview)
    _collect_after_hours(sig, after_hours)
    _collect_alerts(sig, alerts)
    _collect_social(sig, social)
    _collect_divergence(sig, divergence)
    cashtags = _news_cashtags(news)
    _collect_news(sig, cashtags)
    _collect_earnings(sig, indicators)

    symbol_signals = {
        sym: {**_summarize_symbol(entries), **sig.quote.get(sym, {})}
        for sym, entries in sig.by_symbol.items()
    }

    opportunities = []
    for sym, summary in symbol_signals.items():
        # Confluence requirement: a single strong source isn't an "opportunity".
        if len(summary["sources"]) < 2:
            continue
        quote = sig.quote.get(sym, {})
        opportunities.append(
            {
                "symbol": sym,
                "score": summary["score"],
                "direction": summary["direction"],
                "sources": summary["sources"],
                "reasons": summary["reasons"][:6],
                "price": quote.get("price"),
                "percent_change": quote.get("percent_change"),
            }
        )
    opportunities.sort(key=lambda o: o["score"], reverse=True)
    opportunities = opportunities[:MAX_OPPORTUNITIES]

    # Symbols flagged only by price-less sources (divergence, social, news)
    # have no quote yet — fill the shortlist with one batched fetch.
    missing = [o["symbol"] for o in opportunities if o["price"] is None]
    if missing:
        try:
            payload = main_module.watchlist_quote(symbols=",".join(missing))
            fetched = {str(q.get("symbol") or "").upper(): q for q in payload.get("quotes", [])}
            for opp in opportunities:
                quote = fetched.get(opp["symbol"])
                if quote and opp["price"] is None:
                    opp["price"] = _f(quote.get("price"))
                    opp["percent_change"] = (
                        round(_f(quote.get("change_pct")), 2) if _f(quote.get("change_pct")) is not None else None
                    )
                    # symbol_signals was built before enrichment — keep the
                    # watchlist-overlay fallback prices in sync too.
                    entry = symbol_signals.get(opp["symbol"])
                    if entry is not None:
                        entry.setdefault("price", opp["price"])
                        entry.setdefault("percent_change", opp["percent_change"])
        except Exception as exc:
            logger.warning("report: opportunity quote enrichment failed: %s", exc)

    def _top_movers(rows: list[dict[str, Any]], n: int = 3) -> list[dict[str, Any]]:
        out = []
        for row in rows[:n]:
            out.append(
                {
                    "symbol": row.get("symbol"),
                    "name": row.get("name"),
                    "price": _f(row.get("price")),
                    "percent_change": _f(row.get("percent_change")),
                    "volume": _f(row.get("volume")),
                }
            )
        return out

    headline = indicators.get("headline") or {}
    market_pulse = {
        "gainers_count": (overview.get("summary") or {}).get("gainers_count"),
        "losers_count": (overview.get("summary") or {}).get("losers_count"),
        "active_count": (overview.get("summary") or {}).get("active_count"),
        "top_gainers": _top_movers(overview.get("gainers") or []),
        "top_losers": _top_movers(overview.get("losers") or []),
        "most_active": _top_movers(overview.get("active") or []),
        "headline_sentiment_score": _f(headline.get("sentiment_score")),
        "news_velocity_24h": headline.get("news_velocity_24h"),
        "after_hours_movers": [
            {
                "symbol": r.get("symbol"),
                "percent_change": _f(r.get("percent_change")),
                "session_type": r.get("session_type"),
                "market_cap_bucket": r.get("market_cap_bucket"),
            }
            for r in sorted(
                (r for r in after_hours.get("rows", []) or [] if _f(r.get("percent_change")) is not None),
                key=lambda r: abs(_f(r.get("percent_change")) or 0.0),
                reverse=True,
            )[:3]
        ],
    }

    top_stories = [
        {
            "title": r.get("title"),
            "source": r.get("source"),
            "url": r.get("url"),
            "duplicate_count": r.get("duplicate_count"),
        }
        for r in sorted(
            news.get("rows", []) or [], key=lambda r: int(r.get("duplicate_count") or 1), reverse=True
        )[:3]
    ]
    news_summary = {
        "count": news.get("count"),
        "top_stories": top_stories,
        "cashtags": [
            {"symbol": sym, "count": count}
            for sym, count in sorted(cashtags.items(), key=lambda kv: kv[1], reverse=True)[:8]
        ],
    }

    divergence_rows = []
    for group in divergence.get("rows", []) or []:
        stats = (group.get("windows") or {}).get("1") or {}
        median = _f(stats.get("median_pct"))
        bench = _f(stats.get("benchmark_pct"))
        if median is None:
            continue
        divergence_rows.append(
            {
                "group_id": group.get("id"),
                "name": group.get("name"),
                "category": group.get("category"),
                "median_pct": median,
                "benchmark_pct": bench,
                "spread_pct": round(median - bench, 2) if bench is not None else None,
                "best": stats.get("best"),
                "worst": stats.get("worst"),
            }
        )
    divergence_rows.sort(key=lambda r: abs(r["spread_pct"] or 0.0), reverse=True)

    social_rows = [
        {
            "ticker": r.get("ticker"),
            "tier": r.get("candidate_tier"),
            "rank_score": _f(r.get("rank_score")),
            "mention_buzz_ratio": _f(r.get("mention_buzz_ratio")),
            "sentiment_label": r.get("sentiment_label"),
            "mentions": r.get("mentions"),
        }
        for r in (social.get("rows", []) or [])
        if str(r.get("validation_status") or "") == "passed"
    ]
    social_rows.sort(key=lambda r: r["rank_score"] or 0.0, reverse=True)
    social_standouts = {
        "market_regime": social.get("market_regime"),
        "tier_counts": social.get("tier_counts"),
        "rows": social_rows[:6],
    }

    etf_pulse = [
        {
            "symbol": r.get("symbol"),
            "name": r.get("name"),
            "change_pct": _f(r.get("change_pct")),
            "volume": _f(r.get("volume")),
        }
        for r in (etfs.get("rows", []) or [])[:5]
    ]

    return {
        "updated_at": main_module.now_iso(),
        "opportunities": opportunities,
        "symbol_signals": symbol_signals,
        "sections": {
            "market_pulse": market_pulse,
            "news": news_summary,
            "technical_flags": {"rows": alerts.get("rows", []) or [], "updated_at": alerts.get("updated_at")},
            "divergence_highlights": divergence_rows[:6],
            "social_standouts": social_standouts,
            "etf_pulse": etf_pulse,
        },
        "sources": sources,
    }


def _watchlist_overlay(raw_symbols: str, report: dict[str, Any]) -> dict[str, Any]:
    from . import main as main_module

    symbols: list[str] = []
    for token in raw_symbols.split(","):
        token = token.strip().upper()
        if not token or token in symbols:
            continue
        try:
            symbols.append(main_module.validate_symbol(token))
        except Exception:
            continue
        if len(symbols) >= MAX_WATCHLIST_SYMBOLS:
            break
    if not symbols:
        return {"rows": [], "count": 0}

    quotes: dict[str, dict[str, Any]] = {}
    try:
        payload = main_module.watchlist_quote(symbols=",".join(symbols))
        quotes = {str(q.get("symbol") or "").upper(): q for q in payload.get("quotes", [])}
    except Exception as exc:
        logger.warning("report: watchlist quotes failed: %s", exc)

    signal_map = report.get("symbol_signals") or {}
    rows = []
    for sym in symbols:
        quote = quotes.get(sym, {})
        signals = signal_map.get(sym)
        rows.append(
            {
                "symbol": sym,
                "name": quote.get("name"),
                # Live quote first; fall back to the price the report itself
                # collected (e.g. when the provider budget is momentarily spent).
                "price": quote.get("price") if quote.get("price") else (signals or {}).get("price"),
                "change_pct": quote.get("change_pct") if quote.get("change_pct") is not None else (signals or {}).get("percent_change"),
                "score": (signals or {}).get("score", 0),
                "direction": (signals or {}).get("direction"),
                "reasons": (signals or {}).get("reasons", [])[:4],
                "notable": bool(signals and signals.get("score", 0) >= 15),
            }
        )
    rows.sort(key=lambda r: r["score"], reverse=True)
    return {"rows": rows, "count": len(rows)}


@router.get("/api/report/daily")
def report_daily(
    watchlist: str | None = Query(default=None, max_length=400, description="Comma-separated watchlist symbols"),
    refresh: bool = Query(default=False),
) -> dict[str, Any]:
    from . import main as main_module

    payload, meta = main_module.swr_serve(
        "report:daily", _build_report, refresh_after_sec=REPORT_REFRESH_AFTER_SEC, force_refresh=refresh
    )
    out = dict(payload)
    out["watchlist"] = _watchlist_overlay(watchlist, payload) if watchlist else None
    out["cache"] = meta
    return out
