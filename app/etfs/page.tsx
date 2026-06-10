"use client";

import Link from "next/link";
import { useCallback, useEffect, useMemo, useState } from "react";
import { apiGet } from "@/lib/api";
import { TickerBoardLink } from "@/components/ticker-board-link";
import { CardNotice, EmptyTableRow, PanelHeader, PanelSection } from "@/components/ui/data-display";
import { formatInteger, formatPercent, formatPrice, pctColorClass } from "@/lib/format";

type Holding = {
  symbol?: string;
  name?: string;
  weight_pct?: number | null;
};

type HoldingQuote = {
  symbol: string;
  name: string;
  price: number;
  change_pct: number | null;
  volume: number;
};

type HoldingPerfRow = {
  rank: number;
  symbol: string;
  weightPct: number | null | undefined;
  price: number | null;
  changePct: number | null;
  volume: number | null;
};

type EtfRow = {
  rank: number;
  symbol: string;
  name: string;
  price: number;
  change_pct: number | null;
  volume: number;
  avg_volume: number | null;
  industry: string;
  category: string;
  issuer: string;
  largest_holdings: Holding[];
};

type PopularEtfResponse = {
  updated_at: string;
  snapshot_date: string;
  count: number;
  rows: EtfRow[];
};

const LIMIT = 250;

function MoversCard({ title, rows, keyPrefix }: { title: string; rows: HoldingPerfRow[]; keyPrefix: string }) {
  return (
    <div className="card p-3">
      <div className="mb-2 text-xs font-semibold tracking-wide text-[#9aaccc] uppercase">{title}</div>
      {rows.length === 0 ? (
        <div className="text-sm text-[#9aaccc]">No performance data yet.</div>
      ) : (
        <div className="space-y-1 text-sm">
          {rows.map((row) => (
            <div key={`${keyPrefix}-${row.symbol}`} className="flex items-center justify-between gap-2">
              <span>{row.symbol}</span>
              <span className={pctColorClass(row.changePct)}>{formatPercent(row.changePct)}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

export default function PopularEtfsPage() {
  const [data, setData] = useState<PopularEtfResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [industry, setIndustry] = useState("All");
  const [search, setSearch] = useState("");
  const [selectedEtf, setSelectedEtf] = useState<EtfRow | null>(null);
  const [holdingQuotes, setHoldingQuotes] = useState<Record<string, HoldingQuote>>({});
  const [holdingQuotesLoading, setHoldingQuotesLoading] = useState(false);
  const [holdingQuotesError, setHoldingQuotesError] = useState<string | null>(null);

  const load = useCallback(async (refresh = false) => {
    setLoading(true);
    setError(null);
    try {
      const qs = new URLSearchParams({ limit: String(LIMIT), refresh: String(refresh) });
      const res = await apiGet<PopularEtfResponse>(`/api/etfs/popular?${qs.toString()}`);
      setData(res);
    } catch (e) {
      setError(String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load(false);
  }, [load]);

  const industries = useMemo(() => {
    const set = new Set<string>();
    for (const row of data?.rows ?? []) {
      const key = (row.industry || row.category || "").trim();
      if (key) set.add(key);
    }
    return ["All", ...Array.from(set).sort((a, b) => a.localeCompare(b))];
  }, [data]);

  const filteredRows = useMemo(() => {
    if (!data?.rows) return [];
    const q = search.trim().toLowerCase();
    return data.rows.filter((row) => {
      const industryKey = (row.industry || row.category || "").trim();
      const industryMatch = industry === "All" || industryKey === industry;
      const searchMatch = !q || row.symbol.toLowerCase().includes(q) || row.name.toLowerCase().includes(q);
      return industryMatch && searchMatch;
    });
  }, [data, industry, search]);

  useEffect(() => {
    const fetchHoldingsPerformance = async () => {
      if (!selectedEtf) {
        setHoldingQuotes({});
        setHoldingQuotesError(null);
        return;
      }
      const symbols = (selectedEtf.largest_holdings || [])
        .slice(0, 20)
        .map((h) => (h.symbol || "").toUpperCase().trim())
        .filter((s) => /^[A-Z0-9.\-]{1,12}$/.test(s));
      if (symbols.length === 0) {
        setHoldingQuotes({});
        setHoldingQuotesError(null);
        return;
      }

      setHoldingQuotesLoading(true);
      setHoldingQuotesError(null);
      try {
        const qs = encodeURIComponent(symbols.join(","));
        const res = await apiGet<{ quotes: HoldingQuote[] }>(`/api/watchlist/quote?symbols=${qs}`);
        const map: Record<string, HoldingQuote> = {};
        for (const q of res.quotes || []) {
          map[q.symbol.toUpperCase()] = q;
        }
        setHoldingQuotes(map);
      } catch (e) {
        setHoldingQuotes({});
        setHoldingQuotesError(String(e));
      } finally {
        setHoldingQuotesLoading(false);
      }
    };
    void fetchHoldingsPerformance();
  }, [selectedEtf]);

  const holdingRows = useMemo<HoldingPerfRow[]>(() => {
    if (!selectedEtf) return [];
    return (selectedEtf.largest_holdings || []).slice(0, 20).map((h, idx) => {
      const symbol = (h.symbol || "").toUpperCase();
      const quote = holdingQuotes[symbol];
      return {
        rank: idx + 1,
        symbol: h.symbol || h.name || "-",
        weightPct: h.weight_pct,
        price: quote?.price ?? null,
        changePct: quote?.change_pct ?? null,
        volume: quote?.volume ?? null,
      };
    });
  }, [selectedEtf, holdingQuotes]);

  const topPerformers = useMemo(() => {
    return holdingRows
      .filter((r) => r.changePct != null)
      .sort((a, b) => (b.changePct || 0) - (a.changePct || 0))
      .slice(0, 5);
  }, [holdingRows]);

  const topLosers = useMemo(() => {
    return holdingRows
      .filter((r) => r.changePct != null)
      .sort((a, b) => (a.changePct || 0) - (b.changePct || 0))
      .slice(0, 5);
  }, [holdingRows]);

  return (
    <div className="space-y-4">
      <div className="flex flex-col gap-2 md:flex-row md:items-end md:justify-between">
        <div>
          <h1 className="text-2xl font-bold">Popular ETFs</h1>
          <p className="text-sm text-[#9aaccc]">
            Top {LIMIT} ETFs by volume, with cached category, industry focus, and largest holdings.
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <input
            className="input w-[260px]"
            placeholder="Search ETF name or ticker"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
          />
          <select className="input max-w-xs" value={industry} onChange={(e) => setIndustry(e.target.value)}>
            {industries.map((item) => (
              <option key={item} value={item}>
                {item}
              </option>
            ))}
          </select>
          <button className="btn" onClick={() => load(true)} disabled={loading}>
            {loading ? "Refreshing..." : "Refresh Snapshot"}
          </button>
        </div>
      </div>

      {loading ? <CardNotice>Loading ETF snapshot...</CardNotice> : null}
      {error ? <CardNotice tone="danger">{error}</CardNotice> : null}

      <PanelSection>
        <PanelHeader title="Top ETF Table" badge={`${filteredRows.length} symbols`} />
        <div className="table-wrap">
          <table className="table">
            <thead>
              <tr>
                <th>Rank</th>
                <th>Ticker</th>
                <th>Name</th>
                <th>Price</th>
                <th>% Move</th>
                <th>Volume</th>
                <th>Avg Volume</th>
                <th>Industry</th>
                <th>Holdings</th>
              </tr>
            </thead>
            <tbody>
              {!data || filteredRows.length === 0 ? (
                <EmptyTableRow colSpan={9} message="No ETF snapshot available yet." />
              ) : (
                filteredRows.map((row) => (
                  <tr key={row.symbol}>
                    <td>{row.rank}</td>
                    <td>
                      <TickerBoardLink className="font-semibold text-[#5ec4ff]" symbol={row.symbol}>
                        {row.symbol}
                      </TickerBoardLink>
                    </td>
                    <td>{row.name}</td>
                    <td>{formatPrice(row.price)}</td>
                    <td className={pctColorClass(row.change_pct)}>{formatPercent(row.change_pct)}</td>
                    <td>{formatInteger(row.volume)}</td>
                    <td>{formatInteger(row.avg_volume)}</td>
                    <td>{row.industry || row.category || "-"}</td>
                    <td>
                      <button className="btn" onClick={() => setSelectedEtf(row)}>
                        View Top 20
                      </button>
                    </td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>
      </PanelSection>

      {selectedEtf ? (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4" onClick={() => setSelectedEtf(null)}>
          <div className="card w-full max-w-2xl p-4" onClick={(e) => e.stopPropagation()}>
            <div className="mb-3 flex items-start justify-between gap-3">
              <div>
                <div className="text-lg font-semibold">
                  {selectedEtf.symbol} Top Holdings
                </div>
                <div className="text-sm text-[#9aaccc]">{selectedEtf.name}</div>
              </div>
              <div className="flex items-center gap-2">
                {holdingRows.filter((r) => /^[A-Z0-9.\-]{1,12}$/.test(r.symbol)).length >= 3 ? (
                  <Link
                    className="btn"
                    href={`/divergence?symbols=${encodeURIComponent(
                      holdingRows
                        .map((r) => r.symbol)
                        .filter((s) => /^[A-Z0-9.\-]{1,12}$/.test(s))
                        .join(",")
                    )}&benchmark=${encodeURIComponent(selectedEtf.symbol)}`}
                    title={`Find which ${selectedEtf.symbol} holdings are diverging from the rest`}
                  >
                    Divergence Scan
                  </Link>
                ) : null}
                <button className="btn" onClick={() => setSelectedEtf(null)}>
                  Close
                </button>
              </div>
            </div>
            <div className="mb-3 grid gap-3 md:grid-cols-2">
              <MoversCard title="Top Performers" rows={topPerformers} keyPrefix={`${selectedEtf.symbol}-winner`} />
              <MoversCard title="Top Losers" rows={topLosers} keyPrefix={`${selectedEtf.symbol}-loser`} />
            </div>
            <div className="table-wrap max-h-[65vh]">
              <table className="table">
                <thead>
                  <tr>
                    <th>#</th>
                    <th>Holding</th>
                    <th>Weight</th>
                    <th>Price</th>
                    <th>% Move</th>
                    <th>Volume</th>
                  </tr>
                </thead>
                <tbody>
                  {holdingQuotesLoading ? (
                    <EmptyTableRow colSpan={6} message="Loading holdings performance..." />
                  ) : (selectedEtf.largest_holdings || []).length === 0 ? (
                    <EmptyTableRow colSpan={6} message="No holdings available." />
                  ) : (
                    holdingRows.map((row) => (
                      <tr key={`${selectedEtf.symbol}-holding-${row.rank}-${row.symbol}`}>
                        <td>{row.rank}</td>
                        <td>
                          {/^[A-Z0-9.\-]{1,12}$/.test(row.symbol) ? (
                            <TickerBoardLink className="font-semibold text-[#5ec4ff]" symbol={row.symbol}>
                              {row.symbol}
                            </TickerBoardLink>
                          ) : (
                            row.symbol
                          )}
                        </td>
                        <td>{row.weightPct == null ? "-" : `${row.weightPct.toFixed(2)}%`}</td>
                        <td>{formatPrice(row.price)}</td>
                        <td className={pctColorClass(row.changePct)}>
                          {formatPercent(row.changePct)}
                        </td>
                        <td>{formatInteger(row.volume)}</td>
                      </tr>
                    ))
                  )}
                </tbody>
              </table>
            </div>
            {holdingQuotesError ? <div className="mt-2 text-sm text-[#ff6a6a]">{holdingQuotesError}</div> : null}
          </div>
        </div>
      ) : null}
    </div>
  );
}
