"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { TickerBoardLink } from "@/components/ticker-board-link";
import { CardNotice, EmptyTableRow, InlineNotice, PanelHeader, PanelSection } from "@/components/ui/data-display";
import { formatDateTime, formatInteger, formatPercent, formatPrice, pctColorClass } from "@/lib/format";

const WATCHLIST_STORAGE_KEY = "openbb_watchlist_symbols";

type Opportunity = {
  symbol: string;
  score: number;
  direction: "bullish" | "bearish" | "mixed";
  sources: string[];
  reasons: string[];
  price?: number | null;
  percent_change?: number | null;
};

type Mover = {
  symbol?: string;
  name?: string;
  price?: number | null;
  percent_change?: number | null;
  volume?: number | null;
};

type ReportPayload = {
  updated_at?: string;
  opportunities: Opportunity[];
  sections: {
    market_pulse: {
      gainers_count?: number | null;
      losers_count?: number | null;
      active_count?: number | null;
      top_gainers: Mover[];
      top_losers: Mover[];
      most_active: Mover[];
      headline_sentiment_score?: number | null;
      news_velocity_24h?: number | null;
      after_hours_movers: { symbol?: string; percent_change?: number | null; session_type?: string; market_cap_bucket?: string }[];
    };
    news: {
      count?: number | null;
      top_stories: { title?: string; source?: string; url?: string; duplicate_count?: number }[];
      cashtags: { symbol: string; count: number }[];
    };
    technical_flags: {
      rows: { symbol: string; price?: number | null; change_pct?: number | null; rsi14?: number | null; alerts: string[] }[];
    };
    divergence_highlights: {
      group_id?: string;
      name?: string;
      category?: string;
      median_pct?: number | null;
      benchmark_pct?: number | null;
      spread_pct?: number | null;
      best?: { symbol?: string; pct?: number };
      worst?: { symbol?: string; pct?: number };
    }[];
    social_standouts: {
      market_regime?: string;
      tier_counts?: Record<string, number>;
      rows: { ticker?: string; tier?: string; rank_score?: number | null; mention_buzz_ratio?: number | null; sentiment_label?: string; mentions?: number }[];
    };
    etf_pulse: { symbol?: string; name?: string; change_pct?: number | null; volume?: number | null }[];
  };
  watchlist?: {
    rows: { symbol: string; name?: string; price?: number | null; change_pct?: number | null; score: number; direction?: string; reasons: string[]; notable: boolean }[];
    count: number;
  } | null;
  sources: Record<string, { ok: boolean; error?: string; updated_at?: string }>;
  cache?: { age_sec?: number | null; refreshing?: boolean; stale?: boolean; last_error?: string | null };
};

function directionBadge(direction?: string) {
  if (direction === "bullish") return <span className="badge text-[#4fd18b]">Bullish</span>;
  if (direction === "bearish") return <span className="badge text-[#ff6a6a]">Bearish</span>;
  return <span className="badge text-[#ffca6a]">Mixed</span>;
}

function ageLabel(ageSec?: number | null): string {
  if (ageSec == null) return "-";
  if (ageSec < 90) return `${ageSec}s ago`;
  return `${Math.round(ageSec / 60)}m ago`;
}

export default function ReportPage() {
  const [report, setReport] = useState<ReportPayload | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const pollTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const load = useCallback(async (refresh: boolean) => {
    try {
      const params = new URLSearchParams();
      try {
        const raw = localStorage.getItem(WATCHLIST_STORAGE_KEY);
        const symbols: string[] = raw ? JSON.parse(raw) : [];
        if (Array.isArray(symbols) && symbols.length > 0) {
          params.set("watchlist", symbols.slice(0, 30).join(","));
        }
      } catch {
        // unreadable watchlist — market-wide report still works
      }
      if (refresh) params.set("refresh", "true");
      const res = await fetch(`/api/report/daily?${params.toString()}`, { cache: "no-store" });
      if (!res.ok) throw new Error(`Report request failed (${res.status})`);
      const data = (await res.json()) as ReportPayload;
      setReport(data);
      setError(null);
      // While a background rebuild runs, silently re-poll so fresh numbers swap in.
      if (pollTimer.current) clearTimeout(pollTimer.current);
      if (data.cache?.refreshing) {
        pollTimer.current = setTimeout(() => void load(false), 6000);
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load(false);
    return () => {
      if (pollTimer.current) clearTimeout(pollTimer.current);
    };
  }, [load]);

  const cache = report?.cache;
  const sections = report?.sections;
  const failedSources = Object.entries(report?.sources ?? {}).filter(([, v]) => !v.ok);

  return (
    <div className="space-y-4">
      <PanelSection>
        <div className="flex flex-col gap-2 md:flex-row md:items-center md:justify-between">
          <div>
            <h1 className="text-lg font-semibold">Daily Report</h1>
            <p className="text-sm text-[#9aaccc]">
              Cross-tab synthesis of today&apos;s real numbers — market movers, technical flags, social buzz, peer-group
              divergence, headlines and ETFs — ranked by signal confluence.
            </p>
          </div>
          <div className="flex items-center gap-2">
            <span className="badge">
              {cache?.refreshing ? "Rebuilding…" : `Data: ${ageLabel(cache?.age_sec)}`}
            </span>
            <button className="btn" onClick={() => void load(true)}>
              Refresh Now
            </button>
          </div>
        </div>
        {report?.updated_at ? (
          <p className="mt-1 text-xs text-[#9aaccc]">Generated {formatDateTime(report.updated_at)}</p>
        ) : null}
        {cache?.last_error ? (
          <InlineNotice tone="warning">Last background rebuild failed: {cache.last_error} (showing previous report)</InlineNotice>
        ) : null}
        {failedSources.length > 0 ? (
          <InlineNotice tone="warning">
            Sources unavailable this run: {failedSources.map(([k]) => k).join(", ")}
          </InlineNotice>
        ) : null}
      </PanelSection>

      {loading ? <CardNotice>Building today&apos;s report (first run sweeps every data source)...</CardNotice> : null}
      {error ? <CardNotice tone="danger">{error}</CardNotice> : null}

      {report ? (
        <>
          <PanelSection>
            <PanelHeader title="Top Opportunities (confluence-ranked)" badge={`${report.opportunities.length} symbols`} />
            <p className="mb-2 text-xs text-[#9aaccc]">
              Symbols flagged by two or more independent sources today. Score reflects how many signals converge and how
              strong they are — it is a research shortlist, not trading advice.
            </p>
            <div className="table-wrap">
              <table className="table">
                <thead>
                  <tr>
                    <th>#</th>
                    <th>Ticker</th>
                    <th>Score</th>
                    <th>Bias</th>
                    <th>Price</th>
                    <th>Day %</th>
                    <th>Signals</th>
                    <th>Why</th>
                  </tr>
                </thead>
                <tbody>
                  {report.opportunities.length === 0 ? (
                    <EmptyTableRow colSpan={8} message="No multi-source confluence right now." />
                  ) : (
                    report.opportunities.map((opp, idx) => (
                      <tr key={opp.symbol}>
                        <td>{idx + 1}</td>
                        <td>
                          <TickerBoardLink className="font-semibold text-[#5ec4ff]" symbol={opp.symbol} />
                        </td>
                        <td className="font-semibold">{opp.score}</td>
                        <td>{directionBadge(opp.direction)}</td>
                        <td>{formatPrice(opp.price)}</td>
                        <td className={pctColorClass(opp.percent_change)}>{formatPercent(opp.percent_change)}</td>
                        <td className="text-xs text-[#9aaccc]">{opp.sources.join(", ")}</td>
                        <td className="max-w-[420px] text-xs">
                          {opp.reasons.map((reason) => (
                            <div key={reason}>• {reason}</div>
                          ))}
                        </td>
                      </tr>
                    ))
                  )}
                </tbody>
              </table>
            </div>
          </PanelSection>

          {report.watchlist ? (
            <PanelSection>
              <PanelHeader title="Your Watchlist Today" badge={`${report.watchlist.count} symbols`} />
              <div className="table-wrap">
                <table className="table">
                  <thead>
                    <tr>
                      <th>Ticker</th>
                      <th>Price</th>
                      <th>Day %</th>
                      <th>Score</th>
                      <th>Bias</th>
                      <th>Signals Today</th>
                    </tr>
                  </thead>
                  <tbody>
                    {report.watchlist.rows.length === 0 ? (
                      <EmptyTableRow colSpan={6} message="Watchlist is empty — add symbols on the Watchlist tab." />
                    ) : (
                      report.watchlist.rows.map((row) => (
                        <tr key={row.symbol} className={row.notable ? "bg-[#142445]/40" : undefined}>
                          <td>
                            <TickerBoardLink className="font-semibold text-[#5ec4ff]" symbol={row.symbol} />
                          </td>
                          <td>{formatPrice(row.price)}</td>
                          <td className={pctColorClass(row.change_pct)}>{formatPercent(row.change_pct)}</td>
                          <td className="font-semibold">{row.score > 0 ? row.score : "-"}</td>
                          <td>{row.score > 0 ? directionBadge(row.direction) : <span className="text-xs text-[#9aaccc]">quiet</span>}</td>
                          <td className="max-w-[420px] text-xs">
                            {row.reasons.length === 0 ? (
                              <span className="text-[#9aaccc]">No notable signals today</span>
                            ) : (
                              row.reasons.map((reason) => <div key={reason}>• {reason}</div>)
                            )}
                          </td>
                        </tr>
                      ))
                    )}
                  </tbody>
                </table>
              </div>
            </PanelSection>
          ) : null}

          {sections ? (
            <>
              <div className="grid gap-4 lg:grid-cols-2">
                <PanelSection>
                  <PanelHeader title="Market Pulse" />
                  <div className="mb-2 grid grid-cols-3 gap-2 text-center">
                    <div className="card p-2">
                      <div className="text-xs text-[#9aaccc]">Gainers</div>
                      <div className="text-lg font-semibold text-[#4fd18b]">{sections.market_pulse.gainers_count ?? "-"}</div>
                    </div>
                    <div className="card p-2">
                      <div className="text-xs text-[#9aaccc]">Losers</div>
                      <div className="text-lg font-semibold text-[#ff6a6a]">{sections.market_pulse.losers_count ?? "-"}</div>
                    </div>
                    <div className="card p-2">
                      <div className="text-xs text-[#9aaccc]">News Sentiment</div>
                      <div className="text-lg font-semibold">
                        {sections.market_pulse.headline_sentiment_score == null
                          ? "-"
                          : sections.market_pulse.headline_sentiment_score.toFixed(2)}
                      </div>
                    </div>
                  </div>
                  {(["top_gainers", "top_losers", "most_active"] as const).map((key) => (
                    <div key={key} className="mb-1 text-sm">
                      <span className="mr-2 text-xs text-[#9aaccc] uppercase">{key.replace("_", " ")}:</span>
                      {sections.market_pulse[key].map((m) => (
                        <span key={`${key}-${m.symbol}`} className="mr-3 whitespace-nowrap">
                          <TickerBoardLink className="font-semibold text-[#5ec4ff]" symbol={m.symbol ?? ""} />{" "}
                          <span className={pctColorClass(m.percent_change)}>{formatPercent(m.percent_change)}</span>
                        </span>
                      ))}
                    </div>
                  ))}
                  {sections.market_pulse.after_hours_movers.length > 0 ? (
                    <div className="mt-1 text-sm">
                      <span className="mr-2 text-xs text-[#9aaccc] uppercase">Pre/Post:</span>
                      {sections.market_pulse.after_hours_movers.map((m) => (
                        <span key={`ah-${m.symbol}`} className="mr-3 whitespace-nowrap">
                          <TickerBoardLink className="font-semibold text-[#5ec4ff]" symbol={m.symbol ?? ""} />{" "}
                          <span className={pctColorClass(m.percent_change)}>{formatPercent(m.percent_change)}</span>
                          {m.market_cap_bucket ? <span className="text-xs text-[#9aaccc]"> ({m.market_cap_bucket})</span> : null}
                        </span>
                      ))}
                    </div>
                  ) : null}
                </PanelSection>

                <PanelSection>
                  <PanelHeader title="Divergence Highlights" badge="1D spread vs benchmark" />
                  <div className="table-wrap">
                    <table className="table">
                      <thead>
                        <tr>
                          <th>Group</th>
                          <th>Median</th>
                          <th>Bench</th>
                          <th>Best / Worst</th>
                        </tr>
                      </thead>
                      <tbody>
                        {sections.divergence_highlights.length === 0 ? (
                          <EmptyTableRow colSpan={4} message="No divergence data." />
                        ) : (
                          sections.divergence_highlights.map((g) => (
                            <tr key={g.group_id}>
                              <td>
                                <a className="text-[#5ec4ff] hover:underline" href={`/divergence?groups=${g.group_id}`}>
                                  {g.name}
                                </a>
                              </td>
                              <td className={pctColorClass(g.median_pct)}>{formatPercent(g.median_pct)}</td>
                              <td className={pctColorClass(g.benchmark_pct)}>{formatPercent(g.benchmark_pct)}</td>
                              <td className="text-xs">
                                <span className="text-[#4fd18b]">
                                  {g.best?.symbol} {g.best?.pct != null ? `${g.best.pct > 0 ? "+" : ""}${g.best.pct}%` : ""}
                                </span>{" "}
                                /{" "}
                                <span className="text-[#ff6a6a]">
                                  {g.worst?.symbol} {g.worst?.pct != null ? `${g.worst.pct > 0 ? "+" : ""}${g.worst.pct}%` : ""}
                                </span>
                              </td>
                            </tr>
                          ))
                        )}
                      </tbody>
                    </table>
                  </div>
                </PanelSection>

                <PanelSection>
                  <PanelHeader
                    title="Social Standouts"
                    badge={sections.social_standouts.market_regime ? `Regime: ${sections.social_standouts.market_regime}` : undefined}
                  />
                  <div className="table-wrap">
                    <table className="table">
                      <thead>
                        <tr>
                          <th>Ticker</th>
                          <th>Tier</th>
                          <th>Buzz</th>
                          <th>Sentiment</th>
                          <th>Mentions</th>
                        </tr>
                      </thead>
                      <tbody>
                        {sections.social_standouts.rows.length === 0 ? (
                          <EmptyTableRow colSpan={5} message="No qualified social candidates." />
                        ) : (
                          sections.social_standouts.rows.map((row) => (
                            <tr key={row.ticker}>
                              <td>
                                <TickerBoardLink className="font-semibold text-[#5ec4ff]" symbol={row.ticker ?? ""} />
                              </td>
                              <td>{row.tier ?? "-"}</td>
                              <td>{row.mention_buzz_ratio != null ? `${row.mention_buzz_ratio.toFixed(1)}x` : "-"}</td>
                              <td className="capitalize">{row.sentiment_label ?? "-"}</td>
                              <td>{row.mentions ?? "-"}</td>
                            </tr>
                          ))
                        )}
                      </tbody>
                    </table>
                  </div>
                </PanelSection>

                <PanelSection>
                  <PanelHeader title="Technical Flags" badge={`${sections.technical_flags.rows.length} flagged`} />
                  <div className="table-wrap">
                    <table className="table">
                      <thead>
                        <tr>
                          <th>Ticker</th>
                          <th>Day %</th>
                          <th>RSI</th>
                          <th>Flags</th>
                        </tr>
                      </thead>
                      <tbody>
                        {sections.technical_flags.rows.length === 0 ? (
                          <EmptyTableRow colSpan={4} message="No technical alerts on today's movers." />
                        ) : (
                          sections.technical_flags.rows.map((row) => (
                            <tr key={row.symbol}>
                              <td>
                                <TickerBoardLink className="font-semibold text-[#5ec4ff]" symbol={row.symbol} />
                              </td>
                              <td className={pctColorClass(row.change_pct)}>{formatPercent(row.change_pct)}</td>
                              <td>{row.rsi14 != null ? row.rsi14.toFixed(0) : "-"}</td>
                              <td className="text-xs">{row.alerts.join(" | ")}</td>
                            </tr>
                          ))
                        )}
                      </tbody>
                    </table>
                  </div>
                </PanelSection>
              </div>

              <div className="grid gap-4 lg:grid-cols-2">
                <PanelSection>
                  <PanelHeader title="News" badge={`${sections.news.count ?? 0} headlines`} />
                  {sections.news.cashtags.length > 0 ? (
                    <div className="mb-2 text-sm">
                      <span className="mr-2 text-xs text-[#9aaccc] uppercase">Tickers in headlines:</span>
                      {sections.news.cashtags.map((tag) => (
                        <span key={tag.symbol} className="badge mr-2">
                          <TickerBoardLink className="text-[#5ec4ff]" symbol={tag.symbol} /> ×{tag.count}
                        </span>
                      ))}
                    </div>
                  ) : null}
                  <ul className="space-y-1 text-sm">
                    {sections.news.top_stories.map((story) => (
                      <li key={story.url ?? story.title}>
                        <a className="text-[#d9e6ff] hover:underline" href={story.url} target="_blank" rel="noreferrer">
                          {story.title}
                        </a>
                        <span className="ml-2 text-xs text-[#9aaccc]">
                          {story.source}
                          {story.duplicate_count && story.duplicate_count > 1 ? ` · ${story.duplicate_count} sources` : ""}
                        </span>
                      </li>
                    ))}
                  </ul>
                </PanelSection>

                <PanelSection>
                  <PanelHeader title="ETF Pulse" badge="top by volume" />
                  <div className="table-wrap">
                    <table className="table">
                      <thead>
                        <tr>
                          <th>ETF</th>
                          <th>Name</th>
                          <th>Day %</th>
                          <th>Volume</th>
                        </tr>
                      </thead>
                      <tbody>
                        {sections.etf_pulse.length === 0 ? (
                          <EmptyTableRow colSpan={4} message="No ETF snapshot." />
                        ) : (
                          sections.etf_pulse.map((etf) => (
                            <tr key={etf.symbol}>
                              <td>
                                <TickerBoardLink className="font-semibold text-[#5ec4ff]" symbol={etf.symbol ?? ""} />
                              </td>
                              <td className="max-w-[220px] truncate">{etf.name ?? "-"}</td>
                              <td className={pctColorClass(etf.change_pct)}>{formatPercent(etf.change_pct)}</td>
                              <td>{formatInteger(etf.volume)}</td>
                            </tr>
                          ))
                        )}
                      </tbody>
                    </table>
                  </div>
                </PanelSection>
              </div>
            </>
          ) : null}
        </>
      ) : null}
    </div>
  );
}
