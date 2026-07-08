"""Social-sentiment layer: sentiment/ticker lexicon helpers and the source
scrapers (Reddit, Stocktwits, Apewisdom, optional TikTok). Moved verbatim from
main.py; the ranking/build pipeline stays in main and calls these."""

from __future__ import annotations

import json
import logging
import math
import re
import time
from datetime import UTC, datetime
from html import unescape
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote_plus, urlencode
from urllib.request import Request as UrlRequest, urlopen

from .config import *  # noqa: F401,F403 -- lexicons, weights, subreddits, source knobs
from .net import (
    fetch_json_url,
    fetch_text_url,
    reddit_rate_wait,
    source_cooldown_remaining_sec,
    source_record_failure,
    source_record_success,
)
from .utils import now_iso, to_float

logger = logging.getLogger("openbb_api.social")


def mention_context_score(text: str, ticker: str) -> tuple[float, int]:
    src = (text or "").lower()
    cashtag_mentions = len(re.findall(rf"\${re.escape(ticker)}\b", text or "", flags=re.IGNORECASE))
    hits = 0
    for term in FINANCE_CONTEXT_TERMS:
        if term in src:
            hits += 1
    bonus = 2 if cashtag_mentions > 0 else 0
    score = min(1.0, (hits + bonus) / 6.0)
    return score, cashtag_mentions

def extract_tickers(text: str) -> set[str]:
    out: set[str] = set()
    for token in TICKER_PATTERN.findall(text or ""):
        if not token.startswith("$") and token != token.upper():
            continue
        sym = token.lstrip("$").upper()
        if sym in TICKER_EXCLUDE:
            continue
        out.add(sym)
    return out

def classify_sentiment(text: str) -> str:
    src = (text or "").lower()
    bull = sum(src.count(word) for word in SENTIMENT_BULL)
    bear = sum(src.count(word) for word in SENTIMENT_BEAR)
    if bull > bear:
        return "bullish"
    if bear > bull:
        return "bearish"
    return "neutral"

def weighted_source_signal(source_mentions: dict[str, int]) -> float:
    if not source_mentions:
        return 0.0
    score = 0.0
    for source, count in source_mentions.items():
        weight = float(SOURCE_FAMILY_WEIGHTS.get(source, 0.9))
        score += math.log1p(max(0, int(count))) * weight
    return score

def compute_mention_quality(
    *,
    raw_mentions: int,
    qualified_mentions: int,
    unique_authors: int,
    cashtag_mentions: int,
    avg_context: float,
    cross_source_count: int,
    weighted_source_score: float,
    ambiguous: bool,
) -> tuple[float, float]:
    if raw_mentions <= 0:
        return 0.0, 0.0
    qualified_ratio = qualified_mentions / max(1, raw_mentions)
    author_diversity = unique_authors / max(1, raw_mentions)
    cashtag_density = cashtag_mentions / max(1, raw_mentions)
    ambiguity_penalty = 0.2 if ambiguous else 0.0
    concentration_penalty = 0.18 if author_diversity < 0.35 else 0.0
    raw_score = (
        qualified_ratio * 0.42
        + min(1.0, avg_context) * 0.24
        + min(1.0, author_diversity) * 0.14
        + min(1.0, cashtag_density * 2.5) * 0.08
        + min(1.0, weighted_source_score / 3.5) * 0.12
        + (0.03 if cross_source_count >= 2 else 0.0)
        - ambiguity_penalty
        - concentration_penalty
    )
    return max(0.0, min(1.0, raw_score)), author_diversity

def confidence_tier_from_score(score: float) -> str:
    if score >= 2.6:
        return "A"
    if score >= 1.8:
        return "B"
    return "C"

def extract_catalyst_hits(text: str) -> dict[str, int]:
    src = (text or "").lower()
    out: dict[str, int] = {}
    for label, terms in CATALYST_KEYWORDS.items():
        hits = 0
        for term in terms:
            hits += src.count(term)
        if hits > 0:
            out[label] = hits
    return out

def options_context_hits(text: str) -> int:
    src = (text or "").lower()
    return sum(src.count(term) for term in OPTIONS_CONTEXT_TERMS)

def parse_reddit_listing(subreddit: str, sort: str, limit: int = 60) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    payload: dict[str, Any] | None = None
    origin = "reddit_json_public"
    safe_limit = max(5, min(limit, REDDIT_PUBLIC_LIMIT))
    fetch_attempts = 0
    retries_used = 0
    started = time.time()

    last_exc: Exception | None = None
    for attempt in range(max(1, REDDIT_FETCH_RETRIES)):
        fetch_attempts += 1
        try:
            reddit_rate_wait()
            payload = fetch_json_url(
                f"https://www.reddit.com/r/{subreddit}/{sort}.json?limit={safe_limit}&raw_json=1",
                timeout=max(2, REDDIT_FETCH_TIMEOUT_SEC),
                headers={"User-Agent": SOCIAL_REDDIT_USER_AGENT, "Accept": "application/json"},
            )
            retries_used = attempt
            break
        except Exception as exc:
            last_exc = exc
            if attempt < max(1, REDDIT_FETCH_RETRIES) - 1:
                time.sleep(REDDIT_BACKOFF_BASE_SEC * (2 ** attempt))

    if payload is None:
        if last_exc is not None:
            raise last_exc
        raise RuntimeError(f"Reddit listing fetch failed for r/{subreddit}/{sort}")

    rows: list[dict[str, Any]] = []
    for child in payload.get("data", {}).get("children", []):
        d = child.get("data", {})
        title = str(d.get("title") or "")
        body = str(d.get("selftext") or "")
        rows.append(
            {
                "source": f"reddit:{subreddit}",
                "origin": origin,
                "subreddit": subreddit,
                "author": str(d.get("author") or "unknown"),
                "created_utc": float(d.get("created_utc") or 0),
                "engagement": int(d.get("score") or 0) + int(d.get("num_comments") or 0),
                "title": title,
                "text": body,
                "url": f"https://www.reddit.com{d.get('permalink')}" if d.get("permalink") else "",
                "tickers": list(extract_tickers(f"{title}\n{body}")),
            }
        )
    elapsed_ms = int((time.time() - started) * 1000)
    meta = {
        "origin": origin,
        "attempts": fetch_attempts,
        "retries_used": retries_used,
        "elapsed_ms": elapsed_ms,
        "limit": safe_limit,
    }
    return rows, meta

def parse_old_reddit(subreddit: str, limit: int = 40) -> list[dict[str, Any]]:
    html = fetch_text_url(f"https://old.reddit.com/r/{subreddit}/")
    rows: list[dict[str, Any]] = []
    for match in re.finditer(
        r'<a class="title [^"]*"[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
        html,
        flags=re.IGNORECASE | re.DOTALL,
    ):
        title = re.sub(r"<[^>]+>", "", unescape(match.group(2))).strip()
        url = unescape(match.group(1)).strip()
        if not title:
            continue
        rows.append(
            {
                "source": f"reddit:{subreddit}",
                "origin": "old_reddit_html",
                "subreddit": subreddit,
                "author": "unknown",
                "created_utc": time.time(),
                "engagement": 0,
                "title": title,
                "text": "",
                "url": url,
                "tickers": list(extract_tickers(title)),
            }
        )
        if len(rows) >= limit:
            break
    return rows

def fetch_apewisdom_wallstreetbets(limit: int = 80) -> list[dict[str, Any]]:
    if not APEWISDOM_ENABLED:
        return []
    safe_limit = max(5, min(int(limit), max(5, APEWISDOM_MAX_RESULTS)))
    payload = fetch_json_url(
        "https://apewisdom.io/api/v1.0/filter/wallstreetbets/page/1",
        timeout=APEWISDOM_TIMEOUT_SEC,
    )
    items = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        return []
    rows: list[dict[str, Any]] = []
    now_ts = time.time()
    for raw in items[:safe_limit]:
        if not isinstance(raw, dict):
            continue
        ticker = str(raw.get("ticker") or "").upper().strip()
        if not ticker or not SYMBOL_PATTERN.match(ticker):
            continue
        mentions = max(1, int(raw.get("mentions") or 0))
        upvotes = max(0, int(raw.get("upvotes") or 0))
        # Weighted synthetic mention for aggregated ranking source.
        rows.append(
            {
                "source": "apewisdom:wallstreetbets",
                "origin": "apewisdom_api",
                "subreddit": "wallstreetbets",
                "author": "apewisdom",
                "created_utc": now_ts,
                "engagement": upvotes,
                "title": f"{ticker} ranked on ApeWisdom WallStreetBets list",
                "text": f"${ticker} wallstreetbets mentions {mentions} upvotes {upvotes}",
                "url": f"https://apewisdom.io/stocks/{ticker.lower()}",
                "tickers": [ticker],
                "sentiment": "neutral",
                "mention_weight": mentions,
            }
        )
    return rows

def fetch_stocktwits_trending(limit: int = 40) -> list[dict[str, Any]]:
    safe_limit = max(5, min(int(limit), 80))
    payload = fetch_json_url(
        "https://api.stocktwits.com/api/2/trending/symbols.json",
        timeout=STOCKTWITS_TRENDING_TIMEOUT_SEC,
    )
    items = payload.get("symbols") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        return []
    rows: list[dict[str, Any]] = []
    now_ts = time.time()
    for raw in items[:safe_limit]:
        if not isinstance(raw, dict):
            continue
        ticker = str(raw.get("symbol") or "").upper().strip()
        if not ticker or not SYMBOL_PATTERN.match(ticker):
            continue
        watchlist = max(0, int(raw.get("watchlist_count") or 0))
        rows.append(
            {
                "source": "stocktwits:trending",
                "origin": "stocktwits_trending_api",
                "subreddit": None,
                "author": "stocktwits",
                "created_utc": now_ts,
                "engagement": watchlist,
                "title": f"{ticker} trending on Stocktwits",
                "text": f"${ticker} stocktwits trending watchlist {watchlist}",
                "url": str(raw.get("deeplink") or ""),
                "tickers": [ticker],
                "sentiment": "neutral",
                "mention_weight": 2,
            }
        )
    return rows

def fetch_stocktwits_messages(symbol: str) -> list[dict[str, Any]]:
    payload = fetch_json_url(
        f"https://api.stocktwits.com/api/2/streams/symbol/{quote_plus(symbol)}.json",
        timeout=STOCKTWITS_FETCH_TIMEOUT_SEC,
    )
    rows = []
    for msg in payload.get("messages", [])[:40]:
        body = str(msg.get("body") or "")
        sentiment = str((msg.get("entities", {}).get("sentiment") or {}).get("basic") or "").lower()
        if sentiment not in {"bullish", "bearish"}:
            sentiment = classify_sentiment(body)
        created_at = str(msg.get("created_at") or "")
        created_utc = 0.0
        if created_at:
            try:
                created_utc = datetime.fromisoformat(created_at.replace("Z", "+00:00")).timestamp()
            except Exception:
                created_utc = time.time()
        rows.append(
            {
                "source": "stocktwits",
                "origin": "stocktwits_api",
                "subreddit": None,
                "author": str((msg.get("user") or {}).get("username") or "unknown"),
                "created_utc": created_utc,
                "engagement": int((msg.get("likes") or {}).get("total") or 0),
                "title": body[:120],
                "text": body,
                "url": str(msg.get("id") or ""),
                "tickers": [symbol],
                "sentiment": sentiment,
            }
        )
    return rows

def fetch_tiktok_messages(symbols: list[str], limit: int = 120) -> list[dict[str, Any]]:
    if not TIKTOK_PROVIDER_URL or not TIKTOK_PROVIDER_API_KEY or not symbols:
        return []

    body = json.dumps(
        {
            "symbols": sorted(set(s.upper() for s in symbols if s)),
            "limit": max(1, min(500, int(limit))),
        }
    ).encode("utf-8")

    payload = fetch_json_url(
        TIKTOK_PROVIDER_URL,
        timeout=TIKTOK_PROVIDER_TIMEOUT_SEC,
        headers={
            "Authorization": f"Bearer {TIKTOK_PROVIDER_API_KEY}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        data=body,
        method="POST",
    )

    raw_rows: list[Any]
    if isinstance(payload, dict):
        val = payload.get("rows")
        raw_rows = val if isinstance(val, list) else []
    elif isinstance(payload, list):
        raw_rows = payload
    else:
        raw_rows = []

    out: list[dict[str, Any]] = []
    accepted = set(s.upper() for s in symbols if s)
    for row in raw_rows:
        if not isinstance(row, dict):
            continue
        text = str(row.get("text") or row.get("caption") or row.get("title") or "")
        if not text:
            continue
        found = row.get("tickers")
        if isinstance(found, list):
            tickers = [str(t).upper().strip() for t in found if str(t).strip()]
        else:
            tickers = list(extract_tickers(text))
        tickers = [t for t in tickers if t in accepted]
        if not tickers:
            continue

        created_utc = 0.0
        created_raw = row.get("created_utc")
        if created_raw is not None:
            try:
                created_utc = float(created_raw)
            except Exception:
                created_utc = 0.0
        if created_utc <= 0:
            created_at = str(row.get("created_at") or "")
            if created_at:
                try:
                    created_utc = datetime.fromisoformat(created_at.replace("Z", "+00:00")).timestamp()
                except Exception:
                    created_utc = time.time()
            else:
                created_utc = time.time()

        likes = int(row.get("likes") or 0)
        comments = int(row.get("comments") or row.get("comment_count") or 0)
        shares = int(row.get("shares") or row.get("share_count") or 0)
        engagement = likes + comments + shares

        out.append(
            {
                "source": "tiktok",
                "origin": "tiktok_provider",
                "subreddit": None,
                "author": str(row.get("author") or row.get("username") or "unknown"),
                "created_utc": created_utc,
                "engagement": engagement,
                "title": text[:120],
                "text": text,
                "url": str(row.get("url") or ""),
                "tickers": tickers,
                "sentiment": classify_sentiment(text),
            }
        )
    return out
