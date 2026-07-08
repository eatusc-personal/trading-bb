"use client";

import Link from "next/link";
import { useCallback, useEffect, useMemo, useState } from "react";
import { apiGet } from "@/lib/api";
import { MarketTable } from "@/components/market-table";
import { TickerBoardLink } from "@/components/ticker-board-link";
import { CardNotice, EmptyTableRow, InlineNotice, PanelHeader, PanelSection } from "@/components/ui/data-display";
import { formatDateTime, formatInteger, formatPercent, formatPrice, pctColorClass } from "@/lib/format";

type MarketCapBucket = "All" | "Micro" | "Small" | "Mid" | "Large" | "Mega";

type MarketRow = {
  symbol: string;
  name?: string;
  price?: number;
  percent_change?: number | null;
  volume?: number;
  market_cap?: number | null;
  market_cap_bucket?: Exclude<MarketCapBucket, "All"> | null;
};

type OverviewResponse = {
  updated_at: string;
  summary: {
    gainers_count: number;
    losers_count: number;
    active_count: number;
  };
  gainers: MarketRow[];
  losers: MarketRow[];
  active: MarketRow[];
};

type AlertRow = {
  symbol: string;
  price: number | null;
  change_pct: number | null;
  rsi14: number | null;
  alerts: string[];
};

type AlertsResponse = {
  rows: AlertRow[];
  count: number;
  updated_at: string;
};

type AfterHoursRow = {
  symbol: string;
  name?: string;
  asset_group?: string;
  price?: number;
  percent_change?: number | null;
  volume?: number;
  market_cap?: number | null;
  market_cap_bucket?: Exclude<MarketCapBucket, "All"> | null;
};

type AfterHoursResponse = {
  updated_at: string;
  provider: string;
  count: number;
  rows: AfterHoursRow[];
};

type IndicatorHeadline = {
  sentiment_score: number;
  news_velocity_24h: number;
  sample_size: number;
  source: string;
};

type IndicatorAnalystUpdate = {
  symbol: string;
  date: string;
  company: string;
  action: string;
  new_grade: string;
  previous_grade: string;
};

type IndicatorAnalyst = {
  grade_drift_score: number;
  update_count: number;
  recent_updates: IndicatorAnalystUpdate[];
  source: string;
};

type IndicatorEarningsEvent = {
  symbol: string;
  date: string;
  days_until: number;
  eps_estimated: number | null;
  revenue_estimated: number | null;
};

type IndicatorEarnings = {
  next_7d_count: number;
  next_events: IndicatorEarningsEvent[];
  source: string;
};

type IndicatorsResponse = {
  updated_at: string;
  provider: string;
  watch_symbols: string[];
  headline: IndicatorHeadline;
  analyst: IndicatorAnalyst;
  earnings: IndicatorEarnings;
  notes?: Record<string, string>;
};

// Each industry chip opens the Divergence scanner with the matching peer group preselected.
const INDUSTRY_GROUPS: Array<{ label: string; groupId: string }> = [
  { label: "Semiconductors", groupId: "semiconductors" },
  { label: "Software / SaaS", groupId: "software" },
  { label: "Mega-cap Tech", groupId: "mega_tech" },
  { label: "Banks / Brokers", groupId: "banks" },
  { label: "Energy", groupId: "energy" },
  { label: "Biotech", groupId: "biotech" },
  { label: "Pharma / Healthcare", groupId: "pharma_healthcare" },
  { label: "Retail", groupId: "retail" },
  { label: "Airlines", groupId: "airlines" },
  { label: "Payments / Fintech", groupId: "payments_fintech" },
  { label: "Autos / EV", groupId: "autos_ev" },
  { label: "Gold Miners", groupId: "gold_miners" },
  { label: "Defense / Aerospace", groupId: "defense" },
  { label: "REITs", groupId: "reits" },
  { label: "Utilities", groupId: "utilities" },
  { label: "S&P Sectors", groupId: "sector_etfs" },
];

const CAP_FILTERS: MarketCapBucket[] = ["All", "Micro", "Small", "Mid", "Large", "Mega"];

async function loadWithState<T>({
  path,
  setData,
  setLoading,
  setError,
  attempt = 0,
}: {
  path: string;
  setData: (value: T) => void;
  setLoading: (value: boolean) => void;
  setError: (value: string | null) => void;
  attempt?: number;
}) {
  if (attempt === 0) setLoading(true);
  setError(null);
  try {
    const data = await apiGet<T>(path);
    setData(data);
    // Endpoints serve their last result instantly and refresh in the
    // background — silently re-poll a few times so fresh data swaps in.
    const refreshing = Boolean((data as { cache?: { refreshing?: boolean } })?.cache?.refreshing);
    if (refreshing && attempt < 6) {
      setTimeout(() => {
        void loadWithState({ path, setData, setLoading, setError, attempt: attempt + 1 });
      }, 6000);
    }
  } catch (e) {
    setError(String(e));
  } finally {
    if (attempt === 0) setLoading(false);
  }
}

export default function MarketPage() {
  const [overview, setOverview] = useState<OverviewResponse | null>(null);
  const [alerts, setAlerts] = useState<AlertsResponse | null>(null);
  const [afterHours, setAfterHours] = useState<AfterHoursResponse | null>(null);
  const [indicators, setIndicators] = useState<IndicatorsResponse | null>(null);

  const [search, setSearch] = useState("");
  const [capFilter, setCapFilter] = useState<MarketCapBucket>("All");

  const [loadingOverview, setLoadingOverview] = useState(true);
  const [loadingAlerts, setLoadingAlerts] = useState(true);
  const [loadingAfterHours, setLoadingAfterHours] = useState(true);
  const [loadingIndicators, setLoadingIndicators] = useState(true);

  const [errorOverview, setErrorOverview] = useState<string | null>(null);
  const [errorAlerts, setErrorAlerts] = useState<string | null>(null);
  const [errorAfterHours, setErrorAfterHours] = useState<string | null>(null);
  const [errorIndicators, setErrorIndicators] = useState<string | null>(null);

  const loadOverview = useCallback(async () => {
    await loadWithState<OverviewResponse>({
      path: "/api/market/overview",
      setData: setOverview,
      setLoading: setLoadingOverview,
      setError: setErrorOverview,
    });
  }, []);

  const loadAlerts = useCallback(async () => {
    await loadWithState<AlertsResponse>({
      path: "/api/alerts",
      setData: setAlerts,
      setLoading: setLoadingAlerts,
      setError: setErrorAlerts,
    });
  }, []);

  const loadAfterHours = useCallback(async () => {
    await loadWithState<AfterHoursResponse>({
      path: "/api/market/after-hours",
      setData: setAfterHours,
      setLoading: setLoadingAfterHours,
      setError: setErrorAfterHours,
    });
  }, []);

  const loadIndicators = useCallback(async () => {
    await loadWithState<IndicatorsResponse>({
      path: "/api/market/indicators",
      setData: setIndicators,
      setLoading: setLoadingIndicators,
      setError: setErrorIndicators,
    });
  }, []);

  const load = useCallback(() => {
    void loadOverview();
    void loadAlerts();
    void loadAfterHours();
    void loadIndicators();
  }, [loadOverview, loadAlerts, loadAfterHours, loadIndicators]);

  useEffect(() => {
    load();
  }, [load]);

  const filtered = useMemo(() => {
    const q = search.trim().toUpperCase();
    if (!overview) return { gainers: [], losers: [], active: [] };

    const applyFilters = (rows: MarketRow[]) => {
      return rows.filter((r) => {
        const searchMatch = !q || r.symbol.toUpperCase().includes(q) || (r.name || "").toUpperCase().includes(q);
        const capMatch = capFilter === "All" || r.market_cap_bucket === capFilter;
        return searchMatch && capMatch;
      });
    };

    return {
      gainers: applyFilters(overview.gainers),
      losers: applyFilters(overview.losers),
      active: applyFilters(overview.active),
    };
  }, [overview, search, capFilter]);

  const hasAnyError = Boolean(errorOverview || errorAlerts || errorAfterHours || errorIndicators);
  const overviewStats = [
    { label: "Top Gainers", value: loadingOverview ? "..." : (overview?.summary.gainers_count ?? "-") },
    { label: "Top Losers", value: loadingOverview ? "..." : (overview?.summary.losers_count ?? "-") },
    { label: "Most Active", value: loadingOverview ? "..." : (overview?.summary.active_count ?? "-") },
    { label: "Alerts", value: loadingAlerts ? "..." : (alerts?.count ?? "-") },
  ];

  return (
    <div className="space-y-4">
      <div className="flex flex-col items-start justify-between gap-2 md:flex-row md:items-center">
        <div>
          <h1 className="text-2xl font-bold">Market Overview</h1>
          <p className="text-sm text-[#9aaccc]">
            Local market snapshot. Last update: {overview?.updated_at ? formatDateTime(overview.updated_at) : "Loading..."}
          </p>
        </div>
        <div className="flex items-center gap-2">
          <input
            className="input min-w-[250px]"
            placeholder="Search ticker or company"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
          />
          <button className="btn" onClick={load}>
            Refresh
          </button>
        </div>
      </div>

      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
        {overviewStats.map((item) => (
          <div key={item.label} className="card p-3">
            <div className="kpi-label">{item.label}</div>
            <div className="kpi-value">{item.value}</div>
          </div>
        ))}
      </div>

      <PanelSection>
        <PanelHeader title="Market Cap Filter" badge={capFilter} />
        <div className="flex flex-wrap gap-2">
          {CAP_FILTERS.map((cap) => (
            <button key={cap} className={`btn ${capFilter === cap ? "border-[#3a5ea8]" : ""}`} onClick={() => setCapFilter(cap)}>
              {cap}
            </button>
          ))}
        </div>
      </PanelSection>

      {hasAnyError ? (
        <CardNotice tone="danger">
          {errorOverview ? <div>Overview: {errorOverview}</div> : null}
          {errorAlerts ? <div>Alerts: {errorAlerts}</div> : null}
          {errorAfterHours ? <div>After Hours: {errorAfterHours}</div> : null}
          {errorIndicators ? <div>Indicators: {errorIndicators}</div> : null}
        </CardNotice>
      ) : null}

      <PanelSection>
        <PanelHeader title="Sentiment & News Indicators" badge={loadingIndicators ? "Loading..." : indicators?.provider || "-"} />
        {loadingIndicators ? (
          <InlineNotice>Computing headline, analyst, and earnings indicators...</InlineNotice>
        ) : !indicators ? (
          <InlineNotice>Indicators unavailable.</InlineNotice>
        ) : (
          <div className="space-y-3">
            <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
              <div className="card p-3">
                <div className="kpi-label">Headline Sentiment</div>
                <div className="kpi-value">{indicators.headline.sentiment_score.toFixed(2)}</div>
                <div className="text-xs text-[#9aaccc]">source: {indicators.headline.source}</div>
              </div>
              <div className="card p-3">
                <div className="kpi-label">News Velocity (24h)</div>
                <div className="kpi-value">{indicators.headline.news_velocity_24h}</div>
                <div className="text-xs text-[#9aaccc]">sample: {indicators.headline.sample_size}</div>
              </div>
              <div className="card p-3">
                <div className="kpi-label">Analyst Drift</div>
                <div className="kpi-value">{indicators.analyst.grade_drift_score.toFixed(2)}</div>
                <div className="text-xs text-[#9aaccc]">updates: {indicators.analyst.update_count}</div>
              </div>
              <div className="card p-3">
                <div className="kpi-label">Earnings in 7d</div>
                <div className="kpi-value">{indicators.earnings.next_7d_count}</div>
                <div className="text-xs text-[#9aaccc]">source: {indicators.earnings.source}</div>
              </div>
            </div>
            <div className="text-xs text-[#9aaccc]">
              Data sources: headline from RSS blend, analyst from FMP grades, earnings from FMP earnings calendar, social stream via /social page.
            </div>
            <div className="table-wrap">
              <table className="table">
                <thead>
                  <tr>
                    <th>Upcoming Earnings</th>
                    <th>Date</th>
                    <th>Days</th>
                    <th>EPS Est.</th>
                  </tr>
                </thead>
                <tbody>
                  {indicators.earnings.next_events.length === 0 ? (
                    <EmptyTableRow colSpan={4} message="No upcoming tracked earnings events." />
                  ) : (
                    indicators.earnings.next_events.slice(0, 8).map((e) => (
                      <tr key={`${e.symbol}-${e.date}`}>
                        <td>
                          <TickerBoardLink className="font-semibold text-[#5ec4ff]" symbol={e.symbol}>
                            {e.symbol}
                          </TickerBoardLink>
                        </td>
                        <td>{e.date}</td>
                        <td>{e.days_until}</td>
                        <td>{formatPrice(e.eps_estimated)}</td>
                      </tr>
                    ))
                  )}
                </tbody>
              </table>
            </div>
          </div>
        )}
      </PanelSection>

      {loadingOverview ? <CardNotice>Loading top gainers/losers/active...</CardNotice> : null}
      {overview ? (
        <div className="grid gap-4 xl:grid-cols-3">
          <MarketTable title="Top Gainers" rows={filtered.gainers} />
          <MarketTable title="Top Losers" rows={filtered.losers} />
          <MarketTable title="Most Active" rows={filtered.active} />
        </div>
      ) : null}

      <PanelSection>
        <PanelHeader title="After Hours Movers" badge={loadingAfterHours ? "Loading..." : `${afterHours?.count ?? 0} movers`} />
        {loadingAfterHours ? (
          <InlineNotice>Fetching pre/post-market research snapshot...</InlineNotice>
        ) : (
          <div className="table-wrap">
            <table className="table">
              <thead>
                <tr>
                  <th>Symbol</th>
                  <th>Name</th>
                  <th>Asset</th>
                  <th>Price</th>
                  <th>Session %</th>
                  <th>Volume</th>
                </tr>
              </thead>
              <tbody>
                {!afterHours || afterHours.rows.length === 0 ? (
                  <EmptyTableRow colSpan={6} message="No after-hours mover data." />
                ) : (
                  afterHours.rows
                    .filter((row) => capFilter === "All" || row.market_cap_bucket === capFilter)
                    .map((row) => (
                      <tr key={`after-hours-${row.symbol}`}>
                        <td>
                          <TickerBoardLink className="font-semibold text-[#5ec4ff]" symbol={row.symbol}>
                            {row.symbol}
                          </TickerBoardLink>
                        </td>
                        <td className="max-w-[220px] truncate">{row.name || "-"}</td>
                        <td className="capitalize">{row.asset_group || "-"}</td>
                        <td>{formatPrice(row.price)}</td>
                        <td className={pctColorClass(row.percent_change)}>
                          {formatPercent(row.percent_change)}
                        </td>
                        <td>{formatInteger(row.volume)}</td>
                      </tr>
                    ))
                )}
              </tbody>
            </table>
          </div>
        )}
      </PanelSection>

      <PanelSection>
        <PanelHeader title="Alerts / Flags" badge={loadingAlerts ? "Loading..." : `${alerts?.count ?? 0} flagged`} />
        {loadingAlerts ? (
          <InlineNotice>Loading alert signals...</InlineNotice>
        ) : (
          <div className="table-wrap">
            <table className="table">
              <thead>
                <tr>
                  <th>Ticker</th>
                  <th>Price</th>
                  <th>% Move</th>
                  <th>RSI</th>
                  <th>Signals</th>
                </tr>
              </thead>
              <tbody>
                {!alerts || alerts.rows.length === 0 ? (
                  <EmptyTableRow colSpan={5} message="No active signals." />
                ) : (
                  alerts.rows.map((row) => (
                    <tr key={row.symbol}>
                      <td>
                        <TickerBoardLink className="font-semibold text-[#5ec4ff]" symbol={row.symbol}>
                          {row.symbol}
                        </TickerBoardLink>
                      </td>
                      <td>{formatPrice(row.price)}</td>
                      <td className={pctColorClass(row.change_pct)}>
                        {formatPercent(row.change_pct)}
                      </td>
                      <td>{formatPrice(row.rsi14)}</td>
                      <td>{row.alerts.join(" | ")}</td>
                    </tr>
                  ))
                )}
              </tbody>
            </table>
          </div>
        )}
      </PanelSection>

      <PanelSection>
        <PanelHeader title="Industry Divergence" badge={`${INDUSTRY_GROUPS.length} groups`} />
        <p className="mb-2 text-xs text-[#9aaccc]">
          Click an industry to scan its peer group for stocks that are not moving with the rest.
        </p>
        <div className="flex flex-wrap gap-2">
          {INDUSTRY_GROUPS.map((item) => (
            <Link
              key={item.groupId}
              href={`/divergence?groups=${item.groupId}`}
              className="badge px-3 py-1.5 text-xs text-[#d9e6ff] hover:border-[#3a5ea8] hover:text-white"
            >
              {item.label}
            </Link>
          ))}
        </div>
      </PanelSection>
    </div>
  );
}
