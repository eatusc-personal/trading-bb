"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { apiGet } from "@/lib/api";
import { TickerBoardLink } from "@/components/ticker-board-link";

function formatAge(ageSec: number | null | undefined): string {
  if (ageSec == null) return "no data yet";
  if (ageSec < 60) return `${ageSec}s ago`;
  if (ageSec < 3600) return `${Math.floor(ageSec / 60)}m ago`;
  return `${Math.floor(ageSec / 3600)}h ${Math.floor((ageSec % 3600) / 60)}m ago`;
}

type SentimentRow = {
  ticker: string;
  mentions: number;
  raw_mentions: number;
  qualified_mentions: number;
  unique_authors: number;
  cashtag_mentions: number;
  cross_source_confirmed: boolean;
  finance_context_score: number;
  ambiguity_risk: "low" | "high";
  validation_status: "passed" | "failed" | "pending";
  candidate_confidence: number;
  candidate_tier: "A" | "B" | "C";
  mention_quality_score: number;
  weighted_source_score: number;
  tradability_score: number;
  tradability_gate_passed: boolean;
  tradability_gate_reasons: string[];
  spread_bps: number | null;
  annualized_volatility_pct: number | null;
  market_cap_bucket: string | null;
  market_regime: string;
  catalyst_tags: string[];
  options_context_score: number;
  options_context_hits: number;
  engagement: number;
  bullish: number;
  bearish: number;
  neutral: number;
  bullish_pct: number;
  bearish_pct: number;
  sentiment_score: number;
  sentiment_label: "bullish" | "bearish" | "neutral";
  mentions_6h: number;
  mentions_prev_6h: number;
  momentum_ratio: number;
  current_mentions_1h: number;
  avg_mentions_1h_30d: number;
  mention_buzz_ratio: number;
  current_unique_authors_1h: number;
  avg_unique_authors_1h_30d: number;
  author_buzz_ratio: number;
  engagement_zscore: number;
  baseline_samples_30d: number;
  rank_score: number;
  sources: string[];
  why: string[];
};

type SentimentCacheMeta = {
  age_sec: number | null;
  refreshing: boolean;
  refresh_elapsed_sec: number | null;
  stale: boolean;
  refresh_after_sec: number;
  last_error: string | null;
};

type SentimentResponse = {
  updated_at: string;
  status?: string;
  cache?: SentimentCacheMeta;
  count: number;
  rows: SentimentRow[];
  source_counts: Record<string, number>;
  source_health?: Record<
    string,
    {
      attempts: number;
      successes: number;
      failures: number;
      rows: number;
      retry_count: number;
      skipped_count: number;
      total_elapsed_ms: number;
      max_elapsed_ms: number;
      cooldown_remaining_sec: number;
      last_origin?: string | null;
      last_error?: string | null;
    }
  >;
  errors: Record<string, string>;
  notes: Record<string, string>;
};

export default function SocialPage() {
  const [data, setData] = useState<SentimentResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [minMentions, setMinMentions] = useState("4");
  const [sentiment, setSentiment] = useState<"all" | "bullish" | "bearish" | "neutral">("all");
  const [tierFilter, setTierFilter] = useState<"all" | "A" | "AB">("all");
  const [compact, setCompact] = useState(true);

  const pollCount = useRef(0);

  const load = useCallback(async (forceRefresh = false) => {
    if (forceRefresh) pollCount.current = 0;
    setError(null);
    try {
      const res = await apiGet<SentimentResponse>(
        `/api/social/sentiment?limit=60${forceRefresh ? "&refresh=true" : ""}`,
        { timeoutMs: 60000 }
      );
      setData(res);
    } catch (e) {
      setError(String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  // While a background scan is running, poll every 6s so fresh results appear
  // automatically (capped at ~3 minutes of polling).
  useEffect(() => {
    if (!data?.cache?.refreshing) {
      pollCount.current = 0;
      return;
    }
    if (pollCount.current >= 30) return;
    const timer = setTimeout(() => {
      pollCount.current += 1;
      void load(false);
    }, 6000);
    return () => clearTimeout(timer);
  }, [data, load]);

  const filtered = useMemo(() => {
    const q = query.trim().toUpperCase();
    const min = Number(minMentions) || 0;
    const rows = data?.rows ?? [];
    return rows.filter((r) => {
      if (r.mentions < min) return false;
      if (sentiment !== "all" && r.sentiment_label !== sentiment) return false;
      if (tierFilter === "A" && r.candidate_tier !== "A") return false;
      if (tierFilter === "AB" && r.candidate_tier === "C") return false;
      if (q && !r.ticker.includes(q)) return false;
      return true;
    });
  }, [data?.rows, query, minMentions, sentiment, tierFilter]);

  const sourceHealthRows = useMemo(() => {
    const entries = Object.entries(data?.source_health || {});
    entries.sort((a, b) => {
      const failDiff = (b[1].failures || 0) - (a[1].failures || 0);
      if (failDiff !== 0) return failDiff;
      return (b[1].retry_count || 0) - (a[1].retry_count || 0);
    });
    return entries;
  }, [data?.source_health]);

  const redditFastFailSkips = useMemo(() => {
    return sourceHealthRows.filter(
      ([source, stat]) => source.startsWith("reddit:") && String(stat.last_error || "") === "fast_fail_active"
    ).length;
  }, [sourceHealthRows]);

  const fallbackModeActive = useMemo(() => {
    if (!data) return false;
    const hasFallbackSources =
      Number(data.source_counts?.["apewisdom:wallstreetbets"] || 0) > 0 ||
      Number(data.source_counts?.["stocktwits:trending"] || 0) > 0;
    if (!hasFallbackSources) return false;
    if (redditFastFailSkips > 0) return true;
    return Object.keys(data.errors || {}).some((k) => k.startsWith("reddit:"));
  }, [data, redditFastFailSkips]);

  return (
    <div className="space-y-4">
      <div className="flex flex-col items-start justify-between gap-2 md:flex-row md:items-center">
        <div>
          <h1 className="text-2xl font-bold">Social Sentiment</h1>
          <p className="text-sm text-[#9aaccc]">
            Reddit-first social scanner with Stocktwits enrichment. Scans run in the background — this page always loads
            instantly from the last completed scan.
          </p>
        </div>
        <div className="flex items-center gap-2">
          {fallbackModeActive ? (
            <span className="badge px-3 py-2 text-[#ffd166]" title="Using fallback sources because Reddit endpoints were blocked/slow.">
              Fallback Mode Active
            </span>
          ) : null}
          <span
            className={`badge px-3 py-2 ${data?.cache?.refreshing ? "text-[#5ec4ff]" : data?.cache?.stale ? "text-[#ffd166]" : "text-[#9aaccc]"}`}
            title={data?.cache?.last_error ? `Last scan error: ${data.cache.last_error}` : "Age of the data currently shown"}
          >
            {data?.cache?.refreshing
              ? `Scanning… (${data.cache.refresh_elapsed_sec ?? 0}s)`
              : `Data: ${formatAge(data?.cache?.age_sec)}`}
          </span>
          <button className="btn" onClick={() => void load(true)} disabled={Boolean(data?.cache?.refreshing)}>
            {data?.cache?.refreshing ? "Refreshing…" : "Refresh Now"}
          </button>
        </div>
      </div>

      <section className="card p-3">
        <div className="grid gap-2 md:grid-cols-6">
          <input className="input" placeholder="Filter ticker" value={query} onChange={(e) => setQuery(e.target.value.toUpperCase())} />
          <input className="input" placeholder="Min mentions" value={minMentions} onChange={(e) => setMinMentions(e.target.value)} />
          <select className="input" value={sentiment} onChange={(e) => setSentiment(e.target.value as "all" | "bullish" | "bearish" | "neutral")}>
            <option value="all">Sentiment: All</option>
            <option value="bullish">Sentiment: Bullish</option>
            <option value="bearish">Sentiment: Bearish</option>
            <option value="neutral">Sentiment: Neutral</option>
          </select>
          <select className="input" value={tierFilter} onChange={(e) => setTierFilter(e.target.value as "all" | "A" | "AB")}>
            <option value="all">Tier: All</option>
            <option value="AB">Tier: A + B</option>
            <option value="A">Tier: A only</option>
          </select>
          <button className={`btn ${compact ? "border-[#3a5ea8]" : ""}`} onClick={() => setCompact((v) => !v)}>
            {compact ? "Compact View" : "Detailed View"}
          </button>
          <div className="badge h-fit px-3 py-2">{filtered.length} tickers</div>
        </div>
      </section>

      {loading ? <div className="card p-4 text-[#9aaccc]">Loading...</div> : null}
      {error ? <div className="card p-4 text-[#ff6a6a]">{error}</div> : null}
      {!loading && data?.status === "warming" ? (
        <div className="card p-4 text-[#9aaccc]">
          First scan is running in the background (Reddit pacing makes it take ~30-60s). Results will appear here
          automatically{data?.cache?.refresh_elapsed_sec != null ? ` — ${data.cache.refresh_elapsed_sec}s elapsed` : ""}.
        </div>
      ) : null}
      {!loading && data?.cache?.last_error && data.status !== "warming" ? (
        <div className="card p-3 text-xs text-[#ffd166]">
          Last background scan failed ({data.cache.last_error}) — showing previous results. It will retry on the next refresh.
        </div>
      ) : null}

      {!loading && !error ? (
        <section className="card p-3">
          <div className="mb-2 flex items-center justify-between">
            <h2 className="text-sm font-semibold tracking-wide text-[#9aaccc] uppercase">Ranked Opportunities</h2>
            <span className="badge">Actionable sentiment list</span>
          </div>
          <div className="table-wrap">
            <table className="table">
              <thead>
                <tr>
                  <th>Ticker</th>
                  <th>Tier</th>
                  <th>Signal</th>
                  <th>Mentions</th>
                  {!compact ? <th>Authors</th> : null}
                  <th>Buzz</th>
                  <th>Bull%</th>
                  <th>Bear%</th>
                  {!compact ? <th>Momentum</th> : null}
                  {!compact ? <th>Quality</th> : null}
                  {!compact ? <th>Source W</th> : null}
                  <th>Trade</th>
                  <th>Catalyst</th>
                  {!compact ? <th>Confidence</th> : null}
                  {!compact ? <th>Engagement</th> : null}
                  <th>Why Popping</th>
                </tr>
              </thead>
              <tbody>
                {filtered.length === 0 ? (
                  <tr>
                    <td colSpan={compact ? 10 : 16} className="text-[#9aaccc]">No symbols matched filters.</td>
                  </tr>
                ) : (
                  filtered.map((row) => (
                    <tr key={row.ticker}>
                      <td>
                        <TickerBoardLink symbol={row.ticker} className="font-semibold text-[#5ec4ff]">
                          {row.ticker}
                        </TickerBoardLink>
                      </td>
                      <td className={row.candidate_tier === "A" ? "text-[#4fd18b] font-semibold" : row.candidate_tier === "B" ? "text-[#ffd166] font-semibold" : "text-[#ff8c66]"}>
                        {row.candidate_tier}
                      </td>
                      <td className={row.sentiment_label === "bullish" ? "text-[#4fd18b]" : row.sentiment_label === "bearish" ? "text-[#ff6a6a]" : "text-[#9aaccc]"}>
                        {row.sentiment_label}
                      </td>
                      <td>{row.mentions}</td>
                      {!compact ? <td>{row.unique_authors}</td> : null}
                      <td>{row.mention_buzz_ratio.toFixed(2)}x</td>
                      <td>{row.bullish_pct.toFixed(1)}%</td>
                      <td>{row.bearish_pct.toFixed(1)}%</td>
                      {!compact ? <td>{row.momentum_ratio.toFixed(2)}x</td> : null}
                      {!compact ? <td>{row.mention_quality_score.toFixed(2)}</td> : null}
                      {!compact ? <td>{row.weighted_source_score.toFixed(2)}</td> : null}
                      <td className={row.tradability_gate_passed ? "text-[#4fd18b]" : "text-[#ff8c66]"}>
                        {row.tradability_score.toFixed(2)}
                      </td>
                      <td>{row.catalyst_tags.length ? row.catalyst_tags.join(", ") : "-"}</td>
                      {!compact ? <td>{row.candidate_confidence.toFixed(2)}</td> : null}
                      {!compact ? <td>{Intl.NumberFormat().format(row.engagement)}</td> : null}
                      <td className="max-w-[420px]">{row.why.length ? row.why.join(" | ") : "-"}</td>
                    </tr>
                  ))
                )}
              </tbody>
            </table>
          </div>
        </section>
      ) : null}

      {data ? (
        <section className="card p-3">
          <div className="mb-2 flex items-center justify-between">
            <h2 className="text-sm font-semibold tracking-wide text-[#9aaccc] uppercase">Source Health</h2>
            <span className="badge">{sourceHealthRows.length} sources</span>
          </div>
          <div className="table-wrap">
            <table className="table">
              <thead>
                <tr>
                  <th>Source</th>
                  <th>Attempts</th>
                  <th>Success</th>
                  <th>Fail</th>
                  <th>Retries</th>
                  <th>Skipped</th>
                  <th>Rows</th>
                  <th>Avg ms</th>
                  <th>Max ms</th>
                  <th>Cooldown</th>
                  <th>Origin</th>
                  <th>Last Error</th>
                </tr>
              </thead>
              <tbody>
                {sourceHealthRows.length === 0 ? (
                  <tr>
                    <td colSpan={12} className="text-[#9aaccc]">No source health diagnostics available.</td>
                  </tr>
                ) : (
                  sourceHealthRows.map(([source, stat]) => {
                    const attempts = Number(stat.attempts || 0);
                    const avgMs = attempts > 0 ? Math.round(Number(stat.total_elapsed_ms || 0) / attempts) : 0;
                    return (
                      <tr key={source}>
                        <td>{source}</td>
                        <td>{attempts}</td>
                        <td className="text-[#4fd18b]">{stat.successes ?? 0}</td>
                        <td className={(stat.failures || 0) > 0 ? "text-[#ff6a6a]" : ""}>{stat.failures ?? 0}</td>
                        <td>{stat.retry_count ?? 0}</td>
                        <td>{stat.skipped_count ?? 0}</td>
                        <td>{stat.rows ?? 0}</td>
                        <td>{avgMs}</td>
                        <td>{stat.max_elapsed_ms ?? 0}</td>
                        <td>{stat.cooldown_remaining_sec ? `${stat.cooldown_remaining_sec}s` : "-"}</td>
                        <td>{stat.last_origin || "-"}</td>
                        <td className="max-w-[280px] truncate" title={stat.last_error || ""}>{stat.last_error || "-"}</td>
                      </tr>
                    );
                  })
                )}
              </tbody>
            </table>
          </div>
        </section>
      ) : null}

      {data ? (
        <section className="card p-3">
          <h2 className="mb-2 text-sm font-semibold tracking-wide text-[#9aaccc] uppercase">Scanner Notes</h2>
          <ul className="space-y-1 text-sm text-[#9aaccc]">
            {Object.entries(data.notes || {}).map(([k, v]) => (
              <li key={k}>
                <span className="font-semibold text-[#d9e6ff]">{k}:</span> {v}
              </li>
            ))}
          </ul>
        </section>
      ) : null}
    </div>
  );
}
