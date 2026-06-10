"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useCallback, useEffect, useMemo, useState } from "react";
import { PriceVolumeChart } from "@/components/price-volume-chart";
import { apiGet } from "@/lib/api";

type Quote = {
  symbol: string;
  name: string;
  exchange: string;
  last_price: number;
  open: number;
  high: number;
  low: number;
  volume: number;
  prev_close: number;
  // These can be null depending on which provider served the quote
  // (twelvedata/marketstack don't return moving averages or 52w levels).
  year_high: number | null;
  year_low: number | null;
  ma_50d: number | null;
  ma_200d: number | null;
  daily_change_pct: number | null;
  updated_at: string;
};

function fmtNum(value: number | null | undefined, digits = 2) {
  return typeof value === "number" && Number.isFinite(value) && value !== 0 ? value.toFixed(digits) : "-";
}

type Historical = {
  symbol: string;
  range: string;
  updated_at: string;
  rows: number;
  data: Array<{ date: string; open: number; high: number; low: number; close: number; volume: number }>;
  preview: Array<{ date: string; open: number; high: number; low: number; close: number; volume: number }>;
};

const RANGES = ["1M", "3M", "6M", "1Y", "5Y"] as const;

export default function TickerDetailPage() {
  const params = useParams<{ symbol: string }>();
  const symbol = String(params.symbol || "").toUpperCase();

  const [range, setRange] = useState<(typeof RANGES)[number]>("1Y");
  const [quote, setQuote] = useState<Quote | null>(null);
  const [historical, setHistorical] = useState<Historical | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [q, h] = await Promise.all([
        apiGet<Quote>(`/api/ticker/${symbol}/quote`),
        apiGet<Historical>(`/api/ticker/${symbol}/historical?range=${range}`),
      ]);
      setQuote(q);
      setHistorical(h);
    } catch (e) {
      setError(String(e));
    } finally {
      setLoading(false);
    }
  }, [range, symbol]);

  useEffect(() => {
    if (!symbol) return;
    void load();
  }, [load, symbol]);

  const downloadCsv = () => {
    if (!historical?.data?.length) return;
    const header = "date,open,high,low,close,volume\n";
    const rows = historical.data
      .map((r) => `${r.date},${r.open},${r.high},${r.low},${r.close},${r.volume}`)
      .join("\n");
    const blob = new Blob([header + rows], { type: "text/csv;charset=utf-8;" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `${symbol}-${range}.csv`;
    a.click();
    URL.revokeObjectURL(url);
  };

  const kpis = useMemo(() => {
    if (!quote) return [];
    const daily = quote.daily_change_pct;
    const dailyClass = daily == null ? "" : daily >= 0 ? "text-[#4fd18b]" : "text-[#ff6a6a]";
    return [
      ["Last", fmtNum(quote.last_price), ""],
      ["Daily", daily == null ? "-" : `${daily > 0 ? "+" : ""}${daily.toFixed(2)}%`, dailyClass],
      ["Volume", quote.volume ? Intl.NumberFormat().format(quote.volume) : "-", ""],
      ["52W High", fmtNum(quote.year_high), ""],
      ["52W Low", fmtNum(quote.year_low), ""],
      ["MA 50", fmtNum(quote.ma_50d), ""],
      ["MA 200", fmtNum(quote.ma_200d), ""],
    ];
  }, [quote]);

  return (
    <div className="space-y-4">
      <div className="flex flex-col items-start justify-between gap-2 md:flex-row md:items-center">
        <div>
          <h1 className="text-2xl font-bold">{symbol} Detail</h1>
          <p className="text-sm text-[#9aaccc]">{quote?.name || "Loading..."}</p>
        </div>
        <div className="flex items-center gap-2">
          {RANGES.map((r) => (
            <button
              key={r}
              className={`btn ${range === r ? "border-[#3a5ea8]" : ""}`}
              onClick={() => setRange(r)}
            >
              {r}
            </button>
          ))}
          <button className="btn" onClick={downloadCsv}>
            Download CSV
          </button>
          <Link className="btn" href={`/technical?symbol=${encodeURIComponent(symbol)}`}>
            Technical View
          </Link>
        </div>
      </div>

      {loading ? <div className="card p-4 text-[#9aaccc]">Loading ticker data...</div> : null}
      {error ? <div className="card p-4 text-[#ff6a6a]">{error}</div> : null}

      {!loading && !error && quote && historical ? (
        <>
          <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-7">
            {kpis.map(([label, value, className]) => (
              <div key={label} className="card p-3">
                <div className="kpi-label">{label}</div>
                <div className={`text-lg font-semibold ${className}`}>{value}</div>
              </div>
            ))}
          </div>

          <PriceVolumeChart data={historical.data} />

          <section className="card p-3">
            <h2 className="mb-2 text-sm font-semibold tracking-wide text-[#9aaccc] uppercase">Price Table Preview</h2>
            <div className="table-wrap">
              <table className="table">
                <thead>
                  <tr>
                    <th>Date</th>
                    <th>Open</th>
                    <th>High</th>
                    <th>Low</th>
                    <th>Close</th>
                    <th>Volume</th>
                  </tr>
                </thead>
                <tbody>
                  {historical.preview.map((row) => (
                    <tr key={row.date}>
                      <td>{row.date}</td>
                      <td>{row.open.toFixed(2)}</td>
                      <td>{row.high.toFixed(2)}</td>
                      <td>{row.low.toFixed(2)}</td>
                      <td>{row.close.toFixed(2)}</td>
                      <td>{Intl.NumberFormat().format(row.volume)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </section>
        </>
      ) : null}
    </div>
  );
}
