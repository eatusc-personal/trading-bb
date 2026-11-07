"""The heavy live social sentiment scan (Reddit, Stocktwits, ApeWisdom, TikTok)
and its mention helpers. Only ever run from the background refresher in
social_refresh.py, never on a request path."""

from __future__ import annotations

import math
import time
from typing import Any

from .cache import get_cached, set_cached
from .config import (
    AMBIGUOUS_TICKERS,
    APEWISDOM_MAX_RESULTS,
    MARKET_CAP_MULTIPLIER,
    REDDIT_FETCH_RETRIES,
    REDDIT_FETCH_TIMEOUT_SEC,
    SCORING_MODEL_ID,
    SENTIMENT_BEAR,
    SENTIMENT_BULL,
    SOCIAL_MAX_STOCKTWITS_SYMBOLS,
    SOCIAL_QUOTE_VALIDATION_ENABLED,
    SOCIAL_SCAN_TIME_BUDGET_SEC,
    SOCIAL_SKIP_REDDIT_IF_FALLBACK_READY,
    SOCIAL_SOURCE_COOLDOWN_SEC,
    SOCIAL_SOURCE_FAIL_THRESHOLD,
    SOCIAL_SUBREDDITS,
    STOCKTWITS_FETCH_TIMEOUT_SEC,
    TICKER_EXCLUDE,
    TIKTOK_PROVIDER_API_KEY,
    TIKTOK_PROVIDER_URL,
)
from .indicators import compute_indicators
from .market_data import fetch_history, fetch_quote, market_cap_bucket
from .net import source_cooldown_remaining_sec, source_record_failure, source_record_success
from .providers import (
    is_quote_provider_auth_error,
    quote_provider_cooldown_remaining_sec,
    quote_provider_record_auth_failure,
)
from .quote_validation import filter_valid_symbols
from .social import (
    classify_sentiment,
    compute_mention_quality,
    confidence_tier_from_score,
    extract_catalyst_hits,
    fetch_apewisdom_wallstreetbets,
    fetch_stocktwits_messages,
    fetch_stocktwits_trending,
    fetch_tiktok_messages,
    mention_context_score,
    options_context_hits,
    parse_old_reddit,
    parse_reddit_listing,
    weighted_source_signal,
)
from .social_store import persist_social_signal_run, social_baseline_stats, upsert_social_baseline
from .symbol_master import load_symbol_master
from .utils import now_iso, to_float


def append_mentions_from_rows(mentions: list[dict[str, Any]], rows: list[dict[str, Any]]) -> None:
    for row in rows:
        text = f"{row.get('title', '')}\n{row.get('text', '')}"
        sentiment = classify_sentiment(text)
        tickers = row.get("tickers") or []
        for ticker in tickers:
            mentions.append(
                {
                    "ticker": ticker,
                    "source": row.get("source"),
                    "origin": row.get("origin"),
                    "sentiment": sentiment,
                    "author": row.get("author"),
                    "engagement": int(row.get("engagement") or 0),
                    "created_utc": float(row.get("created_utc") or 0),
                    "text": text,
                    "url": row.get("url") or "",
                }
            )


def run_social_sentiment_scan(
    limit: int = 100,
    experiment_id: str | None = None,
) -> dict[str, Any]:
    """The heavy live scan (Reddit + Stocktwits + ApeWisdom with pacing).

    Can take 30-60s of wall-clock network time, so it is only ever called from
    the background refresher behind /api/social/sentiment (routers/social.py),
    never directly on a request path.
    """
    exp = (experiment_id or "").strip()
    cache_key = f"social:sentiment:{limit}:{exp}"
    cached = get_cached(cache_key, 120)
    if cached is not None:
        return cached

    symbol_master, symbol_master_snapshot_date, symbol_master_source = load_symbol_master()
    symbol_master_set = set(symbol_master.keys())

    now_ts = time.time()
    six_hours_ago = now_ts - (6 * 3600)
    twelve_hours_ago = now_ts - (12 * 3600)

    mentions: list[dict[str, Any]] = []
    fetch_errors: dict[str, str] = {}
    source_counts: dict[str, int] = {}
    source_health: dict[str, dict[str, Any]] = {}
    tiktok_enabled = bool(TIKTOK_PROVIDER_URL and TIKTOK_PROVIDER_API_KEY)
    scan_started_at = time.time()
    scan_budget_hit = False

    def within_scan_budget() -> bool:
        return (time.time() - scan_started_at) < max(5.0, SOCIAL_SCAN_TIME_BUDGET_SEC)

    def source_attempt(key: str) -> None:
        slot = source_health.setdefault(
            key,
            {
                "attempts": 0,
                "successes": 0,
                "failures": 0,
                "rows": 0,
                "retry_count": 0,
                "skipped_count": 0,
                "total_elapsed_ms": 0,
                "max_elapsed_ms": 0,
                "last_origin": None,
                "cooldown_remaining_sec": 0,
                "last_error": None,
            },
        )
        slot["attempts"] += 1

    def source_success(
        key: str,
        rows_count: int,
        elapsed_ms: int = 0,
        retries_used: int = 0,
        origin: str | None = None,
    ) -> None:
        slot = source_health.setdefault(
            key,
            {
                "attempts": 0,
                "successes": 0,
                "failures": 0,
                "rows": 0,
                "retry_count": 0,
                "skipped_count": 0,
                "total_elapsed_ms": 0,
                "max_elapsed_ms": 0,
                "last_origin": None,
                "cooldown_remaining_sec": 0,
                "last_error": None,
            },
        )
        slot["successes"] += 1
        slot["rows"] += int(rows_count)
        slot["retry_count"] += int(retries_used)
        slot["total_elapsed_ms"] += int(elapsed_ms)
        slot["max_elapsed_ms"] = max(int(slot.get("max_elapsed_ms") or 0), int(elapsed_ms))
        if origin:
            slot["last_origin"] = origin
        slot["cooldown_remaining_sec"] = source_cooldown_remaining_sec(key)
        source_record_success(key)

    def source_failure(key: str, err: Exception, elapsed_ms: int = 0, retries_used: int = 0) -> None:
        slot = source_health.setdefault(
            key,
            {
                "attempts": 0,
                "successes": 0,
                "failures": 0,
                "rows": 0,
                "retry_count": 0,
                "skipped_count": 0,
                "total_elapsed_ms": 0,
                "max_elapsed_ms": 0,
                "last_origin": None,
                "cooldown_remaining_sec": 0,
                "last_error": None,
            },
        )
        slot["failures"] += 1
        slot["retry_count"] += int(retries_used)
        slot["total_elapsed_ms"] += int(elapsed_ms)
        slot["max_elapsed_ms"] = max(int(slot.get("max_elapsed_ms") or 0), int(elapsed_ms))
        _failures, cooldown = source_record_failure(key)
        slot["cooldown_remaining_sec"] = int(cooldown)
        slot["last_error"] = str(err)

    def source_skipped(key: str, reason: str) -> None:
        slot = source_health.setdefault(
            key,
            {
                "attempts": 0,
                "successes": 0,
                "failures": 0,
                "rows": 0,
                "retry_count": 0,
                "skipped_count": 0,
                "total_elapsed_ms": 0,
                "max_elapsed_ms": 0,
                "last_origin": None,
                "cooldown_remaining_sec": 0,
                "last_error": None,
            },
        )
        slot["skipped_count"] += 1
        slot["cooldown_remaining_sec"] = source_cooldown_remaining_sec(key)
        slot["last_error"] = reason

    fallback_rows_count = 0
    if within_scan_budget():
        source_key = "apewisdom:wallstreetbets"
        source_attempt(source_key)
        started = time.time()
        try:
            ape_rows = fetch_apewisdom_wallstreetbets(limit=APEWISDOM_MAX_RESULTS)
            source_success(source_key, len(ape_rows), elapsed_ms=int((time.time() - started) * 1000), origin="apewisdom_api")
            if ape_rows:
                append_mentions_from_rows(mentions, ape_rows)
                source_counts[source_key] = len(ape_rows)
                fallback_rows_count += len(ape_rows)
        except Exception as exc:
            source_failure(source_key, exc, elapsed_ms=int((time.time() - started) * 1000))
            fetch_errors[source_key] = str(exc)
    else:
        scan_budget_hit = True

    if within_scan_budget():
        source_key = "stocktwits:trending"
        source_attempt(source_key)
        started = time.time()
        try:
            trend_rows = fetch_stocktwits_trending(limit=40)
            source_success(
                source_key,
                len(trend_rows),
                elapsed_ms=int((time.time() - started) * 1000),
                origin="stocktwits_trending_api",
            )
            if trend_rows:
                append_mentions_from_rows(mentions, trend_rows)
                source_counts[source_key] = len(trend_rows)
                fallback_rows_count += len(trend_rows)
        except Exception as exc:
            source_failure(source_key, exc, elapsed_ms=int((time.time() - started) * 1000))
            fetch_errors[source_key] = str(exc)
    else:
        scan_budget_hit = True

    reddit_fast_fail = fallback_rows_count > 0 and SOCIAL_SKIP_REDDIT_IF_FALLBACK_READY
    reddit_failure_streak = 0
    for subreddit in SOCIAL_SUBREDDITS:
        if reddit_fast_fail:
            source_skipped(f"reddit:{subreddit}:hot", "fast_fail_active")
            source_skipped(f"reddit:{subreddit}:new", "fast_fail_active")
            source_skipped(f"reddit:{subreddit}:old", "fast_fail_active")
            continue
        if not within_scan_budget():
            scan_budget_hit = True
            break
        merged: dict[str, dict[str, Any]] = {}
        for sort in ("hot", "new"):
            if not within_scan_budget():
                scan_budget_hit = True
                break
            source_key = f"reddit:{subreddit}:{sort}"
            if source_cooldown_remaining_sec(source_key) > 0:
                source_skipped(source_key, "cooldown_active")
                continue
            source_attempt(source_key)
            started = time.time()
            try:
                rows, fetch_meta = parse_reddit_listing(subreddit, sort, 50)
                source_success(
                    source_key,
                    len(rows),
                    elapsed_ms=int(fetch_meta.get("elapsed_ms") or ((time.time() - started) * 1000)),
                    retries_used=int(fetch_meta.get("retries_used") or 0),
                    origin=str(fetch_meta.get("origin") or "reddit_json_public"),
                )
                if rows:
                    source_counts[f"reddit:{subreddit}:{rows[0].get('origin', 'reddit_unknown')}"] = (
                        source_counts.get(f"reddit:{subreddit}:{rows[0].get('origin', 'reddit_unknown')}", 0) + len(rows)
                    )
                for row in rows:
                    key = row.get("url") or f"{row.get('title')}|{row.get('author')}"
                    merged[key] = row
            except Exception as exc:
                source_failure(source_key, exc, elapsed_ms=int((time.time() - started) * 1000), retries_used=max(0, REDDIT_FETCH_RETRIES - 1))
                fetch_errors[source_key] = str(exc)
                reddit_failure_streak += 1
                err_lower = str(exc).lower()
                if reddit_failure_streak >= 2 and ("403" in err_lower or "timed out" in err_lower or "blocked" in err_lower):
                    reddit_fast_fail = True

        rows = list(merged.values())
        if rows:
            reddit_failure_streak = 0
            append_mentions_from_rows(mentions, rows)
            source_counts[f"reddit:{subreddit}:merged"] = len(rows)
            continue

        if not within_scan_budget():
            scan_budget_hit = True
            break
        source_key = f"reddit:{subreddit}:old"
        if source_cooldown_remaining_sec(source_key) > 0:
            source_skipped(source_key, "cooldown_active")
            continue
        source_attempt(source_key)
        started = time.time()
        try:
            rows_old = parse_old_reddit(subreddit, 35)
            source_success(source_key, len(rows_old), elapsed_ms=int((time.time() - started) * 1000), origin="old_reddit_html")
            append_mentions_from_rows(mentions, rows_old)
            source_counts[source_key] = len(rows_old)
        except Exception as exc:
            source_failure(source_key, exc, elapsed_ms=int((time.time() - started) * 1000))
            fetch_errors[source_key] = str(exc)

    by_ticker: dict[str, dict[str, Any]] = {}
    for mention in mentions:
        ticker = str(mention.get("ticker") or "").upper()
        if not ticker or ticker in TICKER_EXCLUDE:
            continue
        if ticker not in symbol_master_set:
            continue

        mention_weight = max(1, int(mention.get("mention_weight") or 1))
        source_name = str(mention.get("source") or "unknown")
        if source_name.startswith("reddit:"):
            source_family = "reddit"
        elif source_name.startswith("stocktwits"):
            source_family = "stocktwits"
        elif source_name.startswith("apewisdom"):
            source_family = "apewisdom"
        else:
            source_family = source_name
        text = str(mention.get("text") or "")
        context_score, cashtag_mentions = mention_context_score(text, ticker)

        slot = by_ticker.setdefault(
            ticker,
            {
                "ticker": ticker,
                "raw_mentions": 0,
                "qualified_mentions": 0,
                "authors": set(),
                "threads": set(),
                "engagement": 0,
                "max_engagement": 0,
                "bullish": 0,
                "bearish": 0,
                "neutral": 0,
                "mentions_6h": 0,
                "mentions_prev_6h": 0,
                "mentions_1h": 0,
                "authors_1h": set(),
                "engagement_1h": 0,
                "cashtag_mentions": 0,
                "context_sum": 0.0,
                "context_hits": 0,
                "drivers": {},
                "sources": set(),  # source families
                "source_detail": set(),  # specific endpoints
                "source_mentions": {},
                "catalyst_hits": {},
                "options_context_hits": 0,
                "validation_status": "pending",
            },
        )

        slot["raw_mentions"] += mention_weight
        if context_score >= 0.25 or cashtag_mentions > 0:
            slot["qualified_mentions"] += mention_weight
        slot["authors"].add(str(mention.get("author") or "unknown"))
        slot["threads"].add(str(mention.get("url") or ""))
        slot["engagement"] += int(mention.get("engagement") or 0) + max(0, mention_weight - 1)
        slot["max_engagement"] = max(slot["max_engagement"], int(mention.get("engagement") or 0))
        slot["cashtag_mentions"] += (cashtag_mentions * mention_weight)
        slot["context_sum"] += (context_score * mention_weight)
        slot["context_hits"] += mention_weight
        slot["sources"].add(source_family)
        slot["source_detail"].add(source_name)
        slot["source_mentions"][source_family] = int(slot["source_mentions"].get(source_family) or 0) + mention_weight
        for cat, cnt in extract_catalyst_hits(text).items():
            slot["catalyst_hits"][cat] = int(slot["catalyst_hits"].get(cat) or 0) + int(cnt) * mention_weight
        slot["options_context_hits"] += options_context_hits(text) * mention_weight

        s = str(mention.get("sentiment") or "neutral")
        if s == "bullish":
            slot["bullish"] += mention_weight
        elif s == "bearish":
            slot["bearish"] += mention_weight
        else:
            slot["neutral"] += mention_weight

        created_utc = float(mention.get("created_utc") or 0)
        if created_utc >= (now_ts - 3600):
            slot["mentions_1h"] += mention_weight
            slot["authors_1h"].add(str(mention.get("author") or "unknown"))
            slot["engagement_1h"] += int(mention.get("engagement") or 0) + max(0, mention_weight - 1)
        if created_utc >= six_hours_ago:
            slot["mentions_6h"] += mention_weight
        elif created_utc >= twelve_hours_ago:
            slot["mentions_prev_6h"] += mention_weight

        for word in SENTIMENT_BULL + SENTIMENT_BEAR:
            if word in text.lower():
                slot["drivers"][word] = int(slot["drivers"].get(word) or 0) + 1

    prequalified: list[str] = []
    for ticker, row in by_ticker.items():
        unique_authors = len(row["authors"])
        cross_source = len(row["sources"]) >= 2
        has_min_evidence = (
            (row["raw_mentions"] >= 2 and unique_authors >= 2)
            or row["cashtag_mentions"] >= 1
            or row["max_engagement"] >= 120
            or cross_source
        )
        if row["qualified_mentions"] <= 0:
            continue
        if not has_min_evidence:
            continue
        if ticker in AMBIGUOUS_TICKERS or len(ticker) <= 2:
            strong_for_ambiguous = (
                row["cashtag_mentions"] >= 1
                or (unique_authors >= 3 and row["qualified_mentions"] >= 2)
                or cross_source
            )
            if not strong_for_ambiguous:
                continue
        prequalified.append(ticker)

    top_tickers = sorted(prequalified, key=lambda t: by_ticker[t]["raw_mentions"], reverse=True)[:25]

    if tiktok_enabled and top_tickers and within_scan_budget():
        source_key = "tiktok:provider"
        if source_cooldown_remaining_sec(source_key) > 0:
            source_skipped(source_key, "cooldown_active")
        else:
            source_attempt(source_key)
            started = time.time()
            try:
                tiktok_rows = fetch_tiktok_messages(top_tickers, 200)
                source_success(source_key, len(tiktok_rows), elapsed_ms=int((time.time() - started) * 1000), origin="tiktok_provider")
                source_counts[source_key] = len(tiktok_rows)
                for msg in tiktok_rows:
                    for sym in msg.get("tickers") or []:
                        sym_u = str(sym).upper()
                        if sym_u not in by_ticker:
                            continue
                        slot = by_ticker[sym_u]
                        text = str(msg.get("text") or "")
                        context_score, cashtag_mentions = mention_context_score(text, sym_u)
                        slot["raw_mentions"] += 1
                        if context_score >= 0.25 or cashtag_mentions > 0:
                            slot["qualified_mentions"] += 1
                        slot["authors"].add(str(msg.get("author") or "unknown"))
                        slot["engagement"] += int(msg.get("engagement") or 0)
                        slot["max_engagement"] = max(slot["max_engagement"], int(msg.get("engagement") or 0))
                        slot["cashtag_mentions"] += cashtag_mentions
                        slot["context_sum"] += context_score
                        slot["context_hits"] += 1
                        slot["sources"].add("tiktok")
                        slot["source_detail"].add("tiktok")
                        slot["source_mentions"]["tiktok"] = int(slot["source_mentions"].get("tiktok") or 0) + 1
                        for cat, cnt in extract_catalyst_hits(text).items():
                            slot["catalyst_hits"][cat] = int(slot["catalyst_hits"].get(cat) or 0) + int(cnt)
                        slot["options_context_hits"] += options_context_hits(text)
                        sentiment = str(msg.get("sentiment") or "neutral")
                        if sentiment == "bullish":
                            slot["bullish"] += 1
                        elif sentiment == "bearish":
                            slot["bearish"] += 1
                        else:
                            slot["neutral"] += 1
                        created_utc = float(msg.get("created_utc") or 0)
                        if created_utc >= (now_ts - 3600):
                            slot["mentions_1h"] += 1
                            slot["authors_1h"].add(str(msg.get("author") or "unknown"))
                            slot["engagement_1h"] += int(msg.get("engagement") or 0)
                        if created_utc >= six_hours_ago:
                            slot["mentions_6h"] += 1
                        elif created_utc >= twelve_hours_ago:
                            slot["mentions_prev_6h"] += 1
            except Exception as exc:
                source_failure(source_key, exc, elapsed_ms=int((time.time() - started) * 1000))
                fetch_errors[source_key] = str(exc)

    stocktwits_budget = max(1, SOCIAL_MAX_STOCKTWITS_SYMBOLS)
    stocktwits_targets = top_tickers[:stocktwits_budget]
    for sym in stocktwits_targets:
        if not within_scan_budget():
            scan_budget_hit = True
            break
        try:
            twits = fetch_stocktwits_messages(sym)
            source_counts[f"stocktwits:{sym}"] = len(twits)
            for msg in twits:
                slot = by_ticker[sym]
                text = str(msg.get("text") or "")
                context_score, cashtag_mentions = mention_context_score(text, sym)
                slot["raw_mentions"] += 1
                if context_score >= 0.25 or cashtag_mentions > 0:
                    slot["qualified_mentions"] += 1
                slot["authors"].add(str(msg.get("author") or "unknown"))
                slot["engagement"] += int(msg.get("engagement") or 0)
                slot["max_engagement"] = max(slot["max_engagement"], int(msg.get("engagement") or 0))
                slot["cashtag_mentions"] += cashtag_mentions
                slot["context_sum"] += context_score
                slot["context_hits"] += 1
                slot["sources"].add("stocktwits")
                slot["source_detail"].add("stocktwits")
                slot["source_mentions"]["stocktwits"] = int(slot["source_mentions"].get("stocktwits") or 0) + 1
                for cat, cnt in extract_catalyst_hits(text).items():
                    slot["catalyst_hits"][cat] = int(slot["catalyst_hits"].get(cat) or 0) + int(cnt)
                slot["options_context_hits"] += options_context_hits(text)
                sentiment = str(msg.get("sentiment") or "neutral")
                if sentiment == "bullish":
                    slot["bullish"] += 1
                elif sentiment == "bearish":
                    slot["bearish"] += 1
                else:
                    slot["neutral"] += 1
                created_utc = float(msg.get("created_utc") or 0)
                if created_utc >= (now_ts - 3600):
                    slot["mentions_1h"] += 1
                    slot["authors_1h"].add(str(msg.get("author") or "unknown"))
                    slot["engagement_1h"] += int(msg.get("engagement") or 0)
                if created_utc >= six_hours_ago:
                    slot["mentions_6h"] += 1
                elif created_utc >= twelve_hours_ago:
                    slot["mentions_prev_6h"] += 1
        except Exception as exc:
            fetch_errors[f"stocktwits:{sym}"] = str(exc)
        time.sleep(0.25)

    if scan_budget_hit:
        fetch_errors["scan_budget"] = (
            f"Scan exceeded {max(5.0, SOCIAL_SCAN_TIME_BUDGET_SEC):.1f}s budget; returned partial results."
        )

    current_bucket = int(now_ts // 3600)
    baseline_rows: dict[str, dict[str, float]] = {}
    for ticker, row in by_ticker.items():
        baseline_rows[ticker] = {
            "mentions": float(int(row["mentions_1h"])),
            "unique_authors": float(len(row["authors_1h"])),
            "engagement": float(int(row["engagement_1h"])),
        }
    upsert_social_baseline(current_bucket, baseline_rows)
    baseline = social_baseline_stats(list(by_ticker.keys()), now_ts)

    # Phase 3: tiered candidate quality shortlist for validation.
    tier_candidates: list[dict[str, Any]] = []
    for ticker, row in by_ticker.items():
        raw_mentions = int(row["raw_mentions"])
        if raw_mentions <= 0:
            continue
        unique_authors = len(row["authors"])
        qualified_mentions = int(row["qualified_mentions"])
        avg_context = float(row["context_sum"]) / max(1, int(row["context_hits"]))
        source_mentions = dict(row.get("source_mentions") or {})
        weighted_source_score = weighted_source_signal(source_mentions)
        cross_source_count = len(source_mentions)
        cross_source = cross_source_count >= 2
        ambiguous = ticker in AMBIGUOUS_TICKERS or len(ticker) <= 2

        confidence = (
            1.0  # symbol master match
            + min(1.5, int(row["cashtag_mentions"]) * 0.35)
            + avg_context * 1.2
            + (0.6 if unique_authors >= 2 else 0.0)
            + min(0.9, weighted_source_score * 0.25)
            + (0.4 if cross_source else 0.0)
        )
        quality_score, author_diversity = compute_mention_quality(
            raw_mentions=raw_mentions,
            qualified_mentions=qualified_mentions,
            unique_authors=unique_authors,
            cashtag_mentions=int(row["cashtag_mentions"]),
            avg_context=avg_context,
            cross_source_count=cross_source_count,
            weighted_source_score=weighted_source_score,
            ambiguous=ambiguous,
        )
        confidence = confidence + quality_score - (0.7 if ambiguous else 0.0) - (0.5 if author_diversity < 0.35 else 0.0)

        minimum_evidence = (
            (raw_mentions >= 2 and unique_authors >= 2)
            or int(row["cashtag_mentions"]) >= 1
            or int(row["max_engagement"]) >= 120
            or cross_source
        )
        if qualified_mentions <= 0 or not minimum_evidence:
            continue
        if confidence < 1.2:
            continue
        tier_candidates.append(
            {
                "ticker": ticker,
                "confidence": confidence,
                "quality_score": quality_score,
                "tier": confidence_tier_from_score(confidence),
                "weighted_source_score": weighted_source_score,
            }
        )

    tier_candidates.sort(key=lambda x: float(x["confidence"]), reverse=True)
    tier_index = {str(c["ticker"]): c for c in tier_candidates}
    to_validate = [str(c["ticker"]) for c in tier_candidates if str(c["tier"]) in {"A", "B"}][:120]
    if SOCIAL_QUOTE_VALIDATION_ENABLED:
        valid_tickers, validation_diag = filter_valid_symbols(to_validate)
        validation_mode = "quote_validation"
        if validation_diag.get("provider_unavailable") and to_validate:
            # Degraded mode: keep high-confidence, symbol-master-qualified set when quote provider is unavailable.
            valid_tickers = set(to_validate)
            validation_mode = "confidence_fallback"
    else:
        valid_tickers = set(to_validate)
        validation_mode = "confidence_only"
        validation_diag = {
            "provider_unavailable": False,
            "auth_error_count": 0,
            "errors": [],
            "quote_provider_cooldown_sec": quote_provider_cooldown_remaining_sec(),
        }

    market_regime = "unknown"
    try:
        spy_hist = fetch_history("SPY", "1Y", ttl=180)
        spy_ind = compute_indicators(spy_hist)
        spy_last = spy_ind.iloc[-1]
        spy_close = to_float(spy_last.get("close")) or 0.0
        spy_sma200 = to_float(spy_last.get("sma200")) or 0.0
        spy_macd = to_float(spy_last.get("macd")) or 0.0
        spy_signal = to_float(spy_last.get("macd_signal")) or 0.0
        if spy_close > spy_sma200 and spy_macd >= spy_signal:
            market_regime = "risk_on"
        elif spy_close < spy_sma200 and spy_macd < spy_signal:
            market_regime = "risk_off"
        else:
            market_regime = "mixed"
    except Exception:
        market_regime = "unknown"

    tradability_profiles: dict[str, dict[str, Any]] = {}
    quote_map: dict[str, dict[str, Any]] = {}
    valid_list = sorted(list(valid_tickers))
    quote_provider_blocked = bool(validation_diag.get("provider_unavailable")) or quote_provider_cooldown_remaining_sec() > 0
    if valid_list and not quote_provider_blocked:
        try:
            quote_df = fetch_quote(valid_list)
            for _, q in quote_df.iterrows():
                sym = str(q.get("symbol") or "").upper().strip()
                if not sym:
                    continue
                quote_map[sym] = {
                    "last_price": to_float(q.get("last_price")) or to_float(q.get("price")) or 0.0,
                    "volume": to_float(q.get("volume")) or 0.0,
                    "market_cap": to_float(q.get("market_cap")),
                    "bid": to_float(q.get("bid")) or to_float(q.get("bid_price")),
                    "ask": to_float(q.get("ask")) or to_float(q.get("ask_price")),
                }
        except Exception as exc:
            if is_quote_provider_auth_error(exc):
                quote_provider_record_auth_failure()
            quote_map = {}

    volatility_candidates = {str(c["ticker"]) for c in tier_candidates[:40]}
    for ticker in valid_list:
        q = quote_map.get(ticker, {})
        last_price = float(q.get("last_price") or 0.0)
        volume = float(q.get("volume") or 0.0)
        market_cap = q.get("market_cap")
        spread_bps: float | None = None
        bid = q.get("bid")
        ask = q.get("ask")
        if bid is not None and ask is not None and last_price > 0 and ask >= bid > 0:
            spread_bps = ((float(ask) - float(bid)) / last_price) * 10000.0

        annualized_vol_pct: float | None = None
        if ticker in volatility_candidates:
            try:
                hist = fetch_history(ticker, "6M", ttl=180)
                returns = hist["close"].pct_change().dropna()
                if not returns.empty:
                    annualized_vol_pct = float(returns.std() * math.sqrt(252) * 100.0)
            except Exception:
                annualized_vol_pct = None

        liquidity_score = min(1.0, math.log10(max(1.0, volume)) / 7.0)
        spread_penalty = 0.0 if spread_bps is None else min(0.65, max(0.0, (spread_bps - 15.0) / 120.0))
        vol_penalty = 0.0 if annualized_vol_pct is None else min(0.55, max(0.0, (annualized_vol_pct - 55.0) / 120.0))
        mc_bucket = market_cap_bucket(market_cap)
        cap_multiplier = float(MARKET_CAP_MULTIPLIER.get(str(mc_bucket), 0.82))
        regime_multiplier = 1.04 if market_regime == "risk_on" else (0.92 if market_regime == "risk_off" else 1.0)

        tradability_score = max(
            0.1,
            (0.35 + liquidity_score * 0.95 - spread_penalty - vol_penalty) * cap_multiplier * regime_multiplier,
        )
        gate_reasons: list[str] = []
        if volume > 0 and volume < 300_000:
            gate_reasons.append("low_volume")
        if spread_bps is not None and spread_bps > 80:
            gate_reasons.append("wide_spread")
        if annualized_vol_pct is not None and annualized_vol_pct > 130:
            gate_reasons.append("extreme_volatility")
        gate_passed = len(gate_reasons) == 0

        tradability_profiles[ticker] = {
            "score": round(tradability_score, 4),
            "gate_passed": gate_passed,
            "gate_reasons": gate_reasons,
            "liquidity_score": round(liquidity_score, 4),
            "spread_bps": round(spread_bps, 3) if spread_bps is not None else None,
            "annualized_volatility_pct": round(annualized_vol_pct, 3) if annualized_vol_pct is not None else None,
            "market_cap": market_cap,
            "market_cap_bucket": mc_bucket,
            "regime": market_regime,
        }

    ranked = []
    for ticker, row in by_ticker.items():
        raw_mentions = int(row["raw_mentions"])
        if raw_mentions <= 0:
            continue
        unique_authors = len(row["authors"])
        qualified_mentions = int(row["qualified_mentions"])
        bullish = int(row["bullish"])
        bearish = int(row["bearish"])
        neutral = int(row["neutral"])
        mentions_6h = int(row["mentions_6h"])
        mentions_prev_6h = int(row["mentions_prev_6h"])
        mentions_1h = int(row["mentions_1h"])
        unique_authors_1h = len(row["authors_1h"])
        engagement_1h = int(row["engagement_1h"])
        momentum_ratio = mentions_6h / max(1, mentions_prev_6h)
        sentiment_score = (bullish - bearish) / max(1, raw_mentions)
        avg_context = float(row["context_sum"]) / max(1, int(row["context_hits"]))
        cross_source = len(row["sources"]) >= 2
        ambiguity_risk = "high" if (ticker in AMBIGUOUS_TICKERS or len(ticker) <= 2) else "low"
        validation_status = "passed" if ticker in valid_tickers else "failed"
        if validation_status != "passed":
            continue

        b = baseline.get(ticker, {})
        avg_mentions_1h_30d = float(b.get("avg_mentions_1h_30d") or 0.0)
        avg_unique_authors_1h_30d = float(b.get("avg_unique_authors_1h_30d") or 0.0)
        avg_engagement_1h_30d = float(b.get("avg_engagement_1h_30d") or 0.0)
        std_engagement_1h_30d = float(b.get("std_engagement_1h_30d") or 0.0)
        mention_buzz_ratio = mentions_1h / max(1.0, avg_mentions_1h_30d)
        author_buzz_ratio = unique_authors_1h / max(1.0, avg_unique_authors_1h_30d)
        engagement_zscore = (engagement_1h - avg_engagement_1h_30d) / (std_engagement_1h_30d or 1.0)

        bullish = int(row["bullish"])
        bearish = int(row["bearish"])
        sentiment_label = "neutral"
        if sentiment_score >= 0.2:
            sentiment_label = "bullish"
        elif sentiment_score <= -0.2:
            sentiment_label = "bearish"

        source_mentions = dict(row.get("source_mentions") or {})
        weighted_source_score = weighted_source_signal(source_mentions)
        cross_source_count = len(source_mentions)
        candidate_info = tier_index.get(ticker)
        if candidate_info is None:
            quality_score, author_diversity = compute_mention_quality(
                raw_mentions=raw_mentions,
                qualified_mentions=qualified_mentions,
                unique_authors=unique_authors,
                cashtag_mentions=int(row["cashtag_mentions"]),
                avg_context=avg_context,
                cross_source_count=cross_source_count,
                weighted_source_score=weighted_source_score,
                ambiguous=ambiguity_risk == "high",
            )
            candidate_confidence = (
                1.0
                + min(1.5, int(row["cashtag_mentions"]) * 0.35)
                + avg_context * 1.2
                + (0.6 if unique_authors >= 2 else 0.0)
                + min(0.9, weighted_source_score * 0.25)
                + (0.4 if cross_source else 0.0)
                + quality_score
                - (0.7 if ambiguity_risk == "high" else 0.0)
                - (0.5 if author_diversity < 0.35 else 0.0)
            )
            candidate_tier = confidence_tier_from_score(candidate_confidence)
        else:
            candidate_confidence = float(candidate_info["confidence"])
            quality_score = float(candidate_info["quality_score"])
            candidate_tier = str(candidate_info["tier"])
            author_diversity = unique_authors / max(1, raw_mentions)

        tier_bonus = 1.2 if candidate_tier == "A" else (0.4 if candidate_tier == "B" else -0.3)
        weighted_source_bonus = min(3.0, weighted_source_score * 1.05)
        tradability = tradability_profiles.get(ticker) or {
            "score": 0.75,
            "gate_passed": True,
            "gate_reasons": [],
            "liquidity_score": 0.5,
            "spread_bps": None,
            "annualized_volatility_pct": None,
            "market_cap": None,
            "market_cap_bucket": None,
            "regime": market_regime,
        }
        tradability_score = float(tradability.get("score") or 0.75)
        tradability_penalty = 0.0 if bool(tradability.get("gate_passed")) else 4.5
        catalyst_pairs = sorted((row.get("catalyst_hits") or {}).items(), key=lambda kv: kv[1], reverse=True)
        catalyst_tags = [str(k) for k, _v in catalyst_pairs[:3]]
        options_intensity = int(row.get("options_context_hits") or 0)
        options_context_score = min(1.0, options_intensity / max(1.0, raw_mentions * 1.2))

        rank_score = (
            qualified_mentions * 1.4
            + unique_authors * 0.8
            + float(row["engagement"]) / 250.0
            + max(0.0, momentum_ratio - 1.0) * 8.0
            + max(0.0, mention_buzz_ratio - 1.0) * 5.0
            + max(0.0, author_buzz_ratio - 1.0) * 4.0
            + max(-2.0, min(5.0, engagement_zscore)) * 1.8
            + sentiment_score * 10.0
            + candidate_confidence * 3.0
            + quality_score * 7.5
            + weighted_source_bonus
            + tier_bonus
            + tradability_score * 4.0
            + options_context_score * 1.6
            - tradability_penalty
        )

        driver_pairs = sorted(row["drivers"].items(), key=lambda kv: kv[1], reverse=True)
        drivers = [k for k, _ in driver_pairs[:4]]
        explainability = {
            "top_positive_factors": [
                f"tier:{candidate_tier}",
                f"quality:{quality_score:.3f}",
                f"weighted_sources:{weighted_source_score:.3f}",
                f"buzz_ratio:{mention_buzz_ratio:.2f}x",
            ],
            "penalties": [
                f"ambiguity:{ambiguity_risk}",
                f"author_diversity:{author_diversity:.3f}",
                f"tradability_penalty:{tradability_penalty:.2f}",
            ],
            "source_mentions": source_mentions,
        }

        ranked.append(
            {
                "ticker": ticker,
                "mentions": raw_mentions,
                "raw_mentions": raw_mentions,
                "qualified_mentions": qualified_mentions,
                "unique_authors": unique_authors,
                "cashtag_mentions": int(row["cashtag_mentions"]),
                "cross_source_confirmed": cross_source,
                "finance_context_score": round(avg_context, 4),
                "ambiguity_risk": ambiguity_risk,
                "validation_status": validation_status,
                "engagement": int(row["engagement"]),
                "bullish": bullish,
                "bearish": bearish,
                "neutral": neutral,
                "bullish_pct": round((bullish / max(1, raw_mentions)) * 100, 2),
                "bearish_pct": round((bearish / max(1, raw_mentions)) * 100, 2),
                "sentiment_score": round(sentiment_score, 4),
                "sentiment_label": sentiment_label,
                "mentions_6h": mentions_6h,
                "mentions_prev_6h": mentions_prev_6h,
                "momentum_ratio": round(momentum_ratio, 3),
                "current_mentions_1h": mentions_1h,
                "avg_mentions_1h_30d": round(avg_mentions_1h_30d, 4),
                "mention_buzz_ratio": round(mention_buzz_ratio, 4),
                "current_unique_authors_1h": unique_authors_1h,
                "avg_unique_authors_1h_30d": round(avg_unique_authors_1h_30d, 4),
                "author_buzz_ratio": round(author_buzz_ratio, 4),
                "engagement_zscore": round(engagement_zscore, 4),
                "baseline_samples_30d": int(b.get("samples") or 0),
                "candidate_confidence": round(candidate_confidence, 4),
                "candidate_tier": candidate_tier,
                "mention_quality_score": round(quality_score, 4),
                "weighted_source_score": round(weighted_source_score, 4),
                "tradability_score": round(tradability_score, 4),
                "tradability_gate_passed": bool(tradability.get("gate_passed")),
                "tradability_gate_reasons": tradability.get("gate_reasons") or [],
                "spread_bps": tradability.get("spread_bps"),
                "annualized_volatility_pct": tradability.get("annualized_volatility_pct"),
                "market_cap": tradability.get("market_cap"),
                "market_cap_bucket": tradability.get("market_cap_bucket"),
                "market_regime": tradability.get("regime"),
                "catalyst_tags": catalyst_tags,
                "options_context_score": round(options_context_score, 4),
                "options_context_hits": options_intensity,
                "rank_score": round(rank_score, 3),
                "sources": sorted(list(row["sources"])),
                "why": drivers,
                "explainability": explainability,
                "model_id": SCORING_MODEL_ID,
                "asset_type": symbol_master.get(ticker, {}).get("asset_type", "equity"),
                "exchange": symbol_master.get(ticker, {}).get("exchange", ""),
            }
        )

    ranked.sort(key=lambda x: (x["rank_score"], x["qualified_mentions"], x["engagement"]), reverse=True)
    ranked = ranked[:limit]
    tier_counts = {
        "A": sum(1 for row in ranked if str(row.get("candidate_tier")) == "A"),
        "B": sum(1 for row in ranked if str(row.get("candidate_tier")) == "B"),
        "C": sum(1 for row in ranked if str(row.get("candidate_tier")) == "C"),
    }
    tradability_gate_failed_count = sum(1 for row in ranked if not bool(row.get("tradability_gate_passed")))
    signal_run_id = ""
    try:
        signal_run_id = persist_social_signal_run(
            ranked,
            model_id=SCORING_MODEL_ID,
            market_regime=market_regime,
            experiment_id=exp or None,
        )
    except Exception:
        signal_run_id = ""

    payload = {
        "updated_at": now_iso(),
        "count": len(ranked),
        "rows": ranked,
        "model_id": SCORING_MODEL_ID,
        "experiment_id": exp or None,
        "signal_run_id": signal_run_id,
        "tier_counts": tier_counts,
        "market_regime": market_regime,
        "tradability_gate_failed_count": tradability_gate_failed_count,
        "symbol_master_count": len(symbol_master_set),
        "symbol_master_snapshot_date": symbol_master_snapshot_date,
        "symbol_master_source": symbol_master_source,
        "prequalified_count": len(prequalified),
        "valid_tickers_count": len(valid_tickers),
        "source_counts": source_counts,
        "source_health": source_health,
        "validation_diagnostics": {
            "mode": validation_mode,
            "provider_unavailable": bool(validation_diag.get("provider_unavailable")),
            "auth_error_count": int(validation_diag.get("auth_error_count") or 0),
            "errors": validation_diag.get("errors") or [],
        },
        "errors": fetch_errors,
        "notes": {
            "pipeline": "extract -> context/ambiguity -> symbol-master -> minimum-evidence -> tiered-candidate-quality -> weighted-cross-source -> validation -> ranking",
            "normalization": "Adds abnormal-buzz metrics using current 1h vs rolling 30d hourly baseline.",
            "window": "Momentum uses mentions in last 6h vs previous 6h.",
            "sentiment": "Keyword-based sentiment classification (bullish/bearish/neutral).",
            "tiering": "Candidate tiers: A (>=2.6), B (>=1.8), C (<1.8) by calibrated confidence score.",
            "classifier": "Mention-quality classifier blends qualified ratio, context density, author diversity, cashtag density, and weighted source confirmation.",
            "ticker_validation": "Only tier A/B candidates are quote-validated.",
            "tradability": "Adds tradability scoring/gating from liquidity, spread, volatility, market-cap normalization, and market regime multiplier.",
            "catalysts": "Adds catalyst tags from mention text (earnings/analyst/corporate/regulatory/macro) and options context intensity.",
            "reddit_ingestion": "Uses low-rate public Reddit listing endpoints with pacing/retry/backoff, then old.reddit HTML fallback.",
            "reddit_fast_fail": "Skips remaining Reddit sources in-run after repeated blocked/timeout responses.",
            "reddit_timeout_sec": f"{max(2, REDDIT_FETCH_TIMEOUT_SEC)}",
            "stocktwits_timeout_sec": f"{max(1, STOCKTWITS_FETCH_TIMEOUT_SEC)}",
            "reddit_circuit_breaker": f"Per-source cooldown after {SOCIAL_SOURCE_FAIL_THRESHOLD} consecutive failures for {SOCIAL_SOURCE_COOLDOWN_SEC}s.",
            "pushshift": "Disabled in production ingestion path.",
            "apewisdom_fallback": "Uses ApeWisdom WallStreetBets aggregated rankings as a non-Reddit fallback source when available.",
            "stocktwits_trending": "Uses Stocktwits trending symbols endpoint as an additional fast discovery source.",
            "skip_reddit_if_fallback_ready": "true" if SOCIAL_SKIP_REDDIT_IF_FALLBACK_READY else "false",
            "tiktok_source": "Optional provider-based source. Enable with TIKTOK_PROVIDER_URL and TIKTOK_PROVIDER_API_KEY. No scraping path is used.",
            "validation_mode": "Uses quote-validation by default; auto-falls back to high-confidence symbol-master set on temporary provider auth outages.",
            "quote_validation_enabled": "true" if SOCIAL_QUOTE_VALIDATION_ENABLED else "false",
            "quote_provider_cooldown_sec": str(quote_provider_cooldown_remaining_sec()),
            "scan_budget": f"Scan budget {max(5.0, SOCIAL_SCAN_TIME_BUDGET_SEC):.1f}s; Stocktwits fan-out capped at {max(1, SOCIAL_MAX_STOCKTWITS_SYMBOLS)} symbols.",
            "scan_budget_hit": "true" if scan_budget_hit else "false",
            "model_id": SCORING_MODEL_ID,
            "caveat": "Social sentiment is noisy and should be combined with price/volume confirmation.",
        },
    }
    set_cached(cache_key, payload)
    return payload
