"use client";

import Link from "next/link";
import { useCallback, useEffect, useMemo, useState } from "react";
import { apiGet } from "@/lib/api";
import { CardNotice, EmptyTableRow, PanelHeader, PanelSection } from "@/components/ui/data-display";
import { formatDateTime } from "@/lib/format";

type NewsRow = {
  title: string;
  url: string;
  source: string;
  published_at: string | null;
};

type NewsResponse = {
  updated_at: string;
  count: number;
  rows: NewsRow[];
  cache?: { age_sec: number | null; refreshing: boolean; stale: boolean };
};

const LIMITS = [40, 80, 120] as const;

function relativeTime(iso: string | null): string {
  if (!iso) return "-";
  const ts = new Date(iso).getTime();
  if (Number.isNaN(ts)) return "-";
  const diffSec = Math.max(0, Math.round((Date.now() - ts) / 1000));
  if (diffSec < 60) return "just now";
  if (diffSec < 3600) return `${Math.floor(diffSec / 60)}m ago`;
  if (diffSec < 86400) return `${Math.floor(diffSec / 3600)}h ago`;
  return `${Math.floor(diffSec / 86400)}d ago`;
}

function extractCashtags(title: string): string[] {
  const out: string[] = [];
  for (const match of title.matchAll(/\$([A-Z]{1,5})\b/g)) {
    if (!out.includes(match[1])) out.push(match[1]);
  }
  return out;
}

export default function NewsPage() {
  const [data, setData] = useState<NewsResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [sourceFilter, setSourceFilter] = useState("All");
  const [limit, setLimit] = useState<(typeof LIMITS)[number]>(40);

  const load = useCallback(async (attempt = 0) => {
    if (attempt === 0) setLoading(true);
    setError(null);
    try {
      const res = await apiGet<NewsResponse>(`/api/news/headlines?limit=${limit}`);
      setData(res);
      // Headlines are served instantly from the last fetch and refreshed in the
      // background — re-poll briefly so new items swap in without a reload.
      if (res.cache?.refreshing && attempt < 5) {
        setTimeout(() => void load(attempt + 1), 5000);
      }
    } catch (e) {
      setError(String(e));
    } finally {
      if (attempt === 0) setLoading(false);
    }
  }, [limit]);

  useEffect(() => {
    void load();
  }, [load]);

  const sources = useMemo(() => {
    const counts = new Map<string, number>();
    for (const row of data?.rows ?? []) {
      counts.set(row.source, (counts.get(row.source) || 0) + 1);
    }
    return Array.from(counts.entries()).sort((a, b) => b[1] - a[1]);
  }, [data]);

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!data?.rows) return [];
    return data.rows.filter((r) => {
      if (sourceFilter !== "All" && r.source !== sourceFilter) return false;
      if (q && !r.title.toLowerCase().includes(q) && !r.source.toLowerCase().includes(q)) return false;
      return true;
    });
  }, [data, query, sourceFilter]);

  return (
    <div className="space-y-4">
      <div className="flex flex-col items-start justify-between gap-2 md:flex-row md:items-center">
        <div>
          <h1 className="text-2xl font-bold">Headline News</h1>
          <p className="text-sm text-[#9aaccc]">
            Market-moving headlines from free public feeds. Last update: {data?.updated_at ? formatDateTime(data.updated_at) : "Loading..."}
          </p>
        </div>
        <div className="flex items-center gap-2">
          <input
            className="input min-w-[250px]"
            placeholder="Filter headlines"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />
          <select className="input w-24" value={limit} onChange={(e) => setLimit(Number(e.target.value) as (typeof LIMITS)[number])}>
            {LIMITS.map((n) => (
              <option key={n} value={n}>
                {n}
              </option>
            ))}
          </select>
          <button className="btn" onClick={() => void load()}>
            Refresh
          </button>
        </div>
      </div>

      {sources.length > 1 ? (
        <div className="flex flex-wrap gap-2">
          <button
            className={`btn px-3 py-1.5 text-xs ${sourceFilter === "All" ? "border-[#3a5ea8]" : ""}`}
            onClick={() => setSourceFilter("All")}
          >
            All ({data?.rows.length ?? 0})
          </button>
          {sources.map(([source, count]) => (
            <button
              key={source}
              className={`btn px-3 py-1.5 text-xs ${sourceFilter === source ? "border-[#3a5ea8]" : ""}`}
              onClick={() => setSourceFilter((prev) => (prev === source ? "All" : source))}
            >
              {source} ({count})
            </button>
          ))}
        </div>
      ) : null}

      {loading ? <CardNotice>Loading headlines...</CardNotice> : null}
      {error ? <CardNotice tone="danger">{error}</CardNotice> : null}

      {!loading && !error ? (
        <PanelSection>
          <PanelHeader title="Top Headlines" badge={`${filtered.length} rows`} />
          <div className="table-wrap">
            <table className="table">
              <thead>
                <tr>
                  <th>Age</th>
                  <th>Published</th>
                  <th>Source</th>
                  <th>Headline</th>
                </tr>
              </thead>
              <tbody>
                {filtered.length === 0 ? (
                  <EmptyTableRow colSpan={4} message="No headlines match the current filter." />
                ) : (
                  filtered.map((row) => (
                    <tr key={row.url}>
                      <td className="whitespace-nowrap text-[#9aaccc]">{relativeTime(row.published_at)}</td>
                      <td className="whitespace-nowrap">{formatDateTime(row.published_at)}</td>
                      <td>{row.source}</td>
                      <td>
                        <a className="font-semibold text-[#5ec4ff]" href={row.url} target="_blank" rel="noreferrer">
                          {row.title}
                        </a>
                        {extractCashtags(row.title).map((symbol) => (
                          <Link
                            key={`${row.url}-${symbol}`}
                            href={`/ticker/${symbol}`}
                            className="badge ml-2 px-1.5 py-0.5 text-xs text-[#ffca6a] hover:text-white"
                            title={`Open ${symbol} detail`}
                          >
                            {symbol}
                          </Link>
                        ))}
                      </td>
                    </tr>
                  ))
                )}
              </tbody>
            </table>
          </div>
        </PanelSection>
      ) : null}
    </div>
  );
}
