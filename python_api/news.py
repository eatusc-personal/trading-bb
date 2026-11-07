"""Consolidated news headlines from public RSS feeds (optionally augmented by
Intellectia when opted in). The builder is moved verbatim from main.py; the
route wraps it with stale-while-revalidate caching."""

from __future__ import annotations

import json
import re
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from html import unescape
from typing import Any
from urllib.error import URLError
from urllib.request import Request as UrlRequest, urlopen

from .config import *  # noqa: F401,F403 -- intellectia base, honest UA, feed knobs
from .net import fetch_json_url, fetch_text_url
from .utils import now_iso


def _build_news_headlines(limit: int) -> dict[str, Any]:
    feeds = [
        ("MarketWatch Top Stories", "https://feeds.content.dowjones.io/public/rss/mw_topstories"),
        ("Financial Times", "https://www.ft.com/rss/home/uk"),
        ("NYTimes Business", "https://rss.nytimes.com/services/xml/rss/nyt/Business.xml"),
        ("Investing.com Stocks", "https://www.investing.com/rss/news_25.rss"),
        ("SEC Press Releases", "https://www.sec.gov/news/pressreleases.rss"),
    ]
    headers = {
        "User-Agent": HONEST_USER_AGENT,
        "Accept": "application/rss+xml, application/xml, text/xml;q=0.9, */*;q=0.8",
    }
    rows: list[dict[str, Any]] = []
    errors: dict[str, str] = {}

    sec_top_keywords = {
        "charges",
        "charged",
        "fraud",
        "settlement",
        "penalty",
        "penalties",
        "insider",
        "manipulation",
        "crypto",
        "offering",
        "earnings",
        "disclosure",
        "accounting",
        "enforcement",
        "whistleblower",
        "securities",
    }

    # Fetch all feeds concurrently: sequentially this was the slowest part of
    # the News tab (5 feeds x up to 8s timeout each).
    def _fetch_feed(url: str) -> bytes:
        req = UrlRequest(url, headers=headers)
        with urlopen(req, timeout=8) as resp:
            return resp.read()

    feed_bytes: dict[str, bytes | Exception] = {}
    with ThreadPoolExecutor(max_workers=len(feeds)) as feed_pool:
        feed_futures = {feed_pool.submit(_fetch_feed, url): source for source, url in feeds}
        for feed_future in as_completed(feed_futures):
            src = feed_futures[feed_future]
            try:
                feed_bytes[src] = feed_future.result()
            except Exception as exc:
                feed_bytes[src] = exc

    for source, url in feeds:
        try:
            fetched = feed_bytes.get(source)
            if isinstance(fetched, Exception) or fetched is None:
                raise fetched if isinstance(fetched, Exception) else RuntimeError("feed fetch failed")
            root = ET.fromstring(fetched)
            source_rows: list[dict[str, Any]] = []
            for item in root.findall(".//item"):
                title = (item.findtext("title") or "").strip()
                link = (item.findtext("link") or "").strip()
                pub_date_raw = (item.findtext("pubDate") or "").strip()
                if not title or not link:
                    continue

                pub_iso = None
                if pub_date_raw:
                    try:
                        dt = parsedate_to_datetime(pub_date_raw)
                        if dt.tzinfo is None:
                            dt = dt.replace(tzinfo=UTC)
                        pub_iso = dt.astimezone(UTC).isoformat()
                    except Exception:
                        pub_iso = None

                source_rows.append(
                    {
                        "title": title,
                        "url": link,
                        "source": source,
                        "published_at": pub_iso,
                    }
                )

            if source == "SEC Press Releases":
                # Keep only high-impact SEC items and cap list size.
                scored_sec: list[tuple[int, float, dict[str, Any]]] = []
                for r in source_rows:
                    title_l = str(r.get("title") or "").lower()
                    keyword_hits = sum(1 for kw in sec_top_keywords if kw in title_l)
                    ts = 0.0
                    pub = r.get("published_at")
                    if pub:
                        try:
                            ts = datetime.fromisoformat(str(pub).replace("Z", "+00:00")).timestamp()
                        except Exception:
                            ts = 0.0
                    if keyword_hits > 0:
                        scored_sec.append((keyword_hits, ts, r))

                if not scored_sec:
                    # Fallback: keep only the latest 2 SEC rows if no keyword matches.
                    source_rows.sort(key=lambda r: str(r.get("published_at") or ""), reverse=True)
                    rows.extend(source_rows[:2])
                else:
                    scored_sec.sort(key=lambda x: (x[0], x[1]), reverse=True)
                    rows.extend([entry[2] for entry in scored_sec[:4]])
            else:
                rows.extend(source_rows)
        except (URLError, TimeoutError, ET.ParseError, Exception) as exc:
            errors[source] = str(exc)

    def parse_intellectia_ts(value: Any) -> str | None:
        if value is None:
            return None
        raw = str(value).strip()
        if not raw:
            return None
        try:
            # Epoch seconds (or milliseconds) path.
            if raw.isdigit():
                iv = int(raw)
                if iv > 10_000_000_000:
                    iv = iv // 1000
                return datetime.fromtimestamp(iv, tz=UTC).isoformat()
            # ISO timestamp path.
            return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(UTC).isoformat()
        except Exception:
            return None

    def title_from_text(text: str, fallback: str) -> str:
        cleaned = re.sub(r"\s+", " ", (text or "").strip())
        if not cleaned:
            return fallback
        parts = re.split(r"[.!?]\s+", cleaned)
        first = parts[0].strip() if parts else cleaned
        if not first:
            first = cleaned
        return first[:180]

    intellectia_headers = {
        "Accept": "application/json",
        "User-Agent": HONEST_USER_AGENT,
    }
    try:
        flash_payload = fetch_json_url(
            f"{INTELLECTIA_API_BASE}/gateway/v1/news/flash?page=1&page_size=40",
            timeout=10,
            headers=intellectia_headers,
        )
        for row in flash_payload.get("list", []) or []:
            if not isinstance(row, dict):
                continue
            item_id = str(row.get("id") or "").strip()
            text = str(row.get("content") or "").strip()
            title = str(row.get("title") or "").strip()
            if not title:
                title = title_from_text(text, "Intellectia Flash Update")
            if not title:
                continue
            rows.append(
                {
                    "title": title,
                    "url": f"https://intellectia.ai/news-wire#flash-{item_id or title[:24]}",
                    "source": "Intellectia Flash",
                    "published_at": parse_intellectia_ts(row.get("timestamp")),
                }
            )
    except Exception as exc:
        errors["Intellectia Flash"] = str(exc)

    try:
        stock_news_payload = fetch_json_url(
            f"{INTELLECTIA_API_BASE}/gateway/v1/news/list?page=1&page_size=30&type=stock",
            timeout=10,
            headers=intellectia_headers,
        )
        for row in stock_news_payload.get("articles", []) or []:
            if not isinstance(row, dict):
                continue
            title = ""
            title_arr = row.get("title")
            if isinstance(title_arr, list):
                for t in title_arr:
                    if isinstance(t, dict) and str(t.get("lang") or "").lower() == "en":
                        title = str(t.get("text") or "").strip()
                        break
                if not title:
                    for t in title_arr:
                        if isinstance(t, dict) and str(t.get("text") or "").strip():
                            title = str(t.get("text") or "").strip()
                            break
            if not title:
                title = str(row.get("headline") or "").strip()
            if not title:
                continue
            seo_title = ""
            if isinstance(title_arr, list):
                for t in title_arr:
                    if isinstance(t, dict) and str(t.get("seo_title") or "").strip():
                        seo_title = str(t.get("seo_title") or "").strip()
                        break
            link = str(row.get("url") or "").strip()
            if not link and seo_title:
                link = f"https://intellectia.ai/news/stock/{seo_title}"
            if not link:
                doc_id = str(row.get("doc_id") or "").strip()
                link = f"https://intellectia.ai/news/stock#{doc_id or title[:24]}"
            rows.append(
                {
                    "title": title,
                    "url": link,
                    "source": "Intellectia Stock News",
                    "published_at": parse_intellectia_ts(
                        row.get("publish_time") or row.get("published_at") or row.get("timestamp")
                    ),
                }
            )
    except Exception as exc:
        errors["Intellectia Stock News"] = str(exc)

    try:
        brief_payload = fetch_json_url(
            f"{INTELLECTIA_API_BASE}/gateway/v1/news/market-brief?page=1&page_size=12",
            timeout=10,
            headers=intellectia_headers,
        )
        for row in brief_payload.get("data", []) or []:
            if not isinstance(row, dict):
                continue
            title = str(row.get("title") or "").strip()
            content = str(row.get("content") or "").strip()
            if not title:
                title = title_from_text(content, "Intellectia Market Brief")
            if not title:
                continue
            slug = str(row.get("slug") or "").strip()
            link = str(row.get("url") or row.get("web_link") or "").strip()
            if not link:
                link = f"https://intellectia.ai/news/daily-market-brief#{slug or title[:24]}"
            rows.append(
                {
                    "title": title,
                    "url": link,
                    "source": "Intellectia Market Brief",
                    "published_at": parse_intellectia_ts(
                        row.get("timestamp") or row.get("time") or row.get("published_at")
                    ),
                }
            )
    except Exception as exc:
        errors["Intellectia Market Brief"] = str(exc)

    try:
        monitor_payload = fetch_json_url(
            f"{INTELLECTIA_API_BASE}/gateway/v1/news/monitor-news",
            timeout=10,
            headers={**intellectia_headers, "Content-Type": "application/json"},
            data=json.dumps({"page": 1, "page_size": 30}).encode("utf-8"),
            method="POST",
        )
        monitor_data = monitor_payload.get("data") if isinstance(monitor_payload, dict) else {}
        monitor_rows = monitor_data.get("list") if isinstance(monitor_data, dict) else []
        for row in monitor_rows or []:
            if not isinstance(row, dict):
                continue
            title = str(row.get("title") or "").strip()
            content = str(row.get("content") or "").strip()
            if not title:
                title = title_from_text(content, "Intellectia Monitor")
            if not title:
                continue
            item_id = str(row.get("id") or "").strip()
            rows.append(
                {
                    "title": title,
                    "url": f"https://intellectia.ai/news/monitor#{item_id or title[:24]}",
                    "source": "Intellectia Monitor",
                    "published_at": parse_intellectia_ts(
                        row.get("timestamp") or row.get("time") or row.get("published_at")
                    ),
                }
            )
    except Exception as exc:
        errors["Intellectia Monitor"] = str(exc)

    walter_rss_urls = [
        "https://nitter.net/DeItaone/rss",
        "https://nitter.poast.org/DeItaone/rss",
    ]
    walter_loaded = False
    for walter_url in walter_rss_urls:
        try:
            req = UrlRequest(walter_url, headers=headers)
            with urlopen(req, timeout=8) as resp:
                raw_bytes = resp.read()
            if not raw_bytes:
                continue
            root = ET.fromstring(raw_bytes)
            walter_count = 0
            for item in root.findall(".//item"):
                title = (item.findtext("title") or "").strip()
                link = (item.findtext("link") or "").strip()
                pub_date_raw = (item.findtext("pubDate") or "").strip()
                if not title or not link:
                    continue
                pub_iso = None
                if pub_date_raw:
                    try:
                        dt = parsedate_to_datetime(pub_date_raw)
                        if dt.tzinfo is None:
                            dt = dt.replace(tzinfo=UTC)
                        pub_iso = dt.astimezone(UTC).isoformat()
                    except Exception:
                        pub_iso = None
                rows.append(
                    {
                        "title": title,
                        "url": link,
                        "source": "Walter Bloomberg (Mirror)",
                        "published_at": pub_iso,
                    }
                )
                walter_count += 1
                if walter_count >= 25:
                    break
            if walter_count > 0:
                walter_loaded = True
                break
        except Exception:
            continue
    if not walter_loaded:
        errors["Walter Bloomberg (Mirror)"] = "No reachable public mirror RSS feed."

    source_priority = {
        "Walter Bloomberg (Mirror)": 96,
        "Intellectia Flash": 90,
        "Intellectia Monitor": 86,
        "Intellectia Market Brief": 84,
        "Intellectia Stock News": 82,
        "SEC Press Releases": 110,
        "MarketWatch Top Stories": 75,
        "Financial Times": 70,
        "NYTimes Business": 60,
        "Investing.com Stocks": 55,
    }

    title_stopwords = {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "for",
        "from",
        "has",
        "in",
        "into",
        "is",
        "it",
        "its",
        "of",
        "on",
        "or",
        "that",
        "the",
        "to",
        "was",
        "were",
        "will",
        "with",
    }

    def canonical_url(url: str) -> str:
        try:
            # Strip tracking params and normalize trailing slash so syndicated links map together.
            base = url.split("#", 1)[0]
            parts = base.split("?", 1)
            path = parts[0].rstrip("/")
            if len(parts) == 1:
                return path
            kept = []
            for qp in parts[1].split("&"):
                if not qp:
                    continue
                k = qp.split("=", 1)[0].lower()
                if k.startswith("utm_") or k in {"guccounter", "guce_referrer", "guce_referrer_sig", "ocid", "cmpid"}:
                    continue
                kept.append(qp)
            if not kept:
                return path
            kept.sort()
            return f"{path}?{'&'.join(kept)}"
        except Exception:
            return url

    def normalized_title(title: str) -> str:
        src = re.sub(r"[^a-z0-9\\s]", " ", title.lower())
        src = re.sub(r"\\s+", " ", src).strip()
        return src

    def title_tokens(title: str) -> set[str]:
        out: set[str] = set()
        for token in normalized_title(title).split(" "):
            if len(token) <= 2:
                continue
            if token in title_stopwords:
                continue
            out.add(token)
        return out

    def title_is_similar(a: str, b: str) -> bool:
        if not a or not b:
            return False
        na = normalized_title(a)
        nb = normalized_title(b)
        if na == nb:
            return True
        ta = title_tokens(a)
        tb = title_tokens(b)
        if not ta or not tb:
            return False
        inter = len(ta.intersection(tb))
        union = len(ta.union(tb))
        jaccard = inter / max(1, union)
        overlap = inter / max(1, min(len(ta), len(tb)))
        # Catch same-story rewrites while avoiding over-merge of loosely related macro headlines.
        return (jaccard >= 0.67 and inter >= 3) or (overlap >= 0.82 and inter >= 4)

    dedup: dict[str, dict[str, Any]] = {}
    for row in rows:
        key = canonical_url(str(row.get("url") or ""))
        if key not in dedup:
            row["related_sources"] = [row.get("source")]
            row["related_titles"] = [row.get("title")]
            row["duplicate_count"] = 1
            dedup[key] = row
            continue
        existing = dedup[key]
        existing["duplicate_count"] = int(existing.get("duplicate_count") or 1) + 1
        rel_sources = set(existing.get("related_sources") or [])
        rel_sources.add(row.get("source"))
        existing["related_sources"] = sorted([str(s) for s in rel_sources if s])
        rel_titles = existing.get("related_titles") or []
        if row.get("title") and row.get("title") not in rel_titles:
            rel_titles.append(row.get("title"))
            existing["related_titles"] = rel_titles[:6]

    def priority_score(row: dict[str, Any]) -> tuple[float, float]:
        source = str(row.get("source") or "")
        base = float(source_priority.get(source, 40))

        ts = 0.0
        pub = row.get("published_at")
        if pub:
            try:
                ts = datetime.fromisoformat(str(pub).replace("Z", "+00:00")).timestamp()
            except Exception:
                ts = 0.0

        # Recency bonus: max +25 points at publish-time, decays to 0 after 24h.
        recency_bonus = 0.0
        if ts > 0:
            age_hours = max(0.0, (time.time() - ts) / 3600.0)
            recency_bonus = max(0.0, 25.0 - age_hours)

        return base + recency_bonus, ts

    ranked_rows = list(dedup.values())
    ranked_rows.sort(key=lambda row: priority_score(row), reverse=True)
    consolidated: list[dict[str, Any]] = []
    for row in ranked_rows:
        merged = False
        for kept in consolidated:
            if title_is_similar(str(row.get("title") or ""), str(kept.get("title") or "")):
                kept["duplicate_count"] = int(kept.get("duplicate_count") or 1) + int(row.get("duplicate_count") or 1)
                rel_sources = set(kept.get("related_sources") or [])
                rel_sources.update(row.get("related_sources") or [row.get("source")])
                kept["related_sources"] = sorted([str(s) for s in rel_sources if s])
                rel_titles = kept.get("related_titles") or []
                for t in row.get("related_titles") or [row.get("title")]:
                    if t and t not in rel_titles:
                        rel_titles.append(t)
                kept["related_titles"] = rel_titles[:6]
                merged = True
                break
        if not merged:
            consolidated.append(row)
        if len(consolidated) >= limit:
            break

    sorted_rows = consolidated[:limit]

    payload: dict[str, Any] = {
        "updated_at": now_iso(),
        "count": len(sorted_rows),
        "rows": sorted_rows,
    }
    if errors:
        payload["errors"] = errors
    return payload
