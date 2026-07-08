"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { apiGet } from "@/lib/api";
import { TickerBoardLink } from "@/components/ticker-board-link";
import { CardNotice, EmptyTableRow, PanelHeader, PanelSection } from "@/components/ui/data-display";
import { formatInteger, formatPercent, formatPrice, pctColorClass } from "@/lib/format";

type QuoteRow = {
  symbol: string;
  name: string;
  price: number;
  change_pct: number | null;
  volume: number;
};

type WatchlistResponse = {
  quotes: QuoteRow[];
  count: number;
  updated_at: string;
};

const STORAGE_KEY = "openbb_watchlist_symbols";

export default function WatchlistPage() {
  const [symbol, setSymbol] = useState("");
  const [list, setList] = useState<string[]>([]);
  const [data, setData] = useState<WatchlistResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (raw) {
      try {
        const parsed = JSON.parse(raw) as string[];
        setList(parsed);
      } catch {
        setList([]);
      }
    }
  }, []);

  const refreshQuotes = useCallback(async (symbols = list) => {
    if (!symbols.length) return;
    setLoading(true);
    setError(null);
    try {
      const res = await apiGet<WatchlistResponse>(`/api/watchlist/quote?symbols=${encodeURIComponent(symbols.join(","))}`);
      setData(res);
    } catch (e) {
      setError(String(e));
    } finally {
      setLoading(false);
    }
  }, [list]);

  useEffect(() => {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(list));
    if (list.length > 0) {
      void refreshQuotes(list);
    } else {
      setData(null);
    }
  }, [list, refreshQuotes]);

  // Accepts one or many symbols ("AAPL" or "AAPL, MSFT NVDA"); invalid tokens are rejected.
  const addSymbol = () => {
    const tokens = symbol
      .split(/[\s,;]+/)
      .map((s) => s.trim().toUpperCase())
      .filter(Boolean);
    if (!tokens.length) return;
    const valid = tokens.filter((s) => /^[A-Z0-9.\-]{1,12}$/.test(s));
    const invalid = tokens.filter((s) => !/^[A-Z0-9.\-]{1,12}$/.test(s));
    if (invalid.length) {
      setError(`Ignored invalid symbol${invalid.length > 1 ? "s" : ""}: ${invalid.join(", ")}`);
    } else {
      setError(null);
    }
    if (valid.length) {
      setList((prev) => [...prev, ...valid.filter((s) => !prev.includes(s))]);
    }
    setSymbol("");
  };

  const removeSymbol = (target: string) => {
    setList((prev) => prev.filter((s) => s !== target));
  };

  return (
    <div className="space-y-4">
      <div>
        <h1 className="text-2xl font-bold">Watchlist</h1>
        <p className="text-sm text-[#9aaccc]">Stored in browser localStorage and refreshed from your local API.</p>
      </div>

      <PanelSection>
        <div className="flex flex-wrap gap-2">
          <input
            className="input max-w-xs"
            value={symbol}
            onChange={(e) => setSymbol(e.target.value.toUpperCase())}
            onKeyDown={(e) => {
              if (e.key === "Enter") addSymbol();
            }}
            placeholder="Add ticker(s): AAPL or AAPL, MSFT, NVDA"
          />
          <button className="btn" onClick={addSymbol}>Add</button>
          <button className="btn" onClick={() => refreshQuotes()} disabled={list.length === 0}>
            Refresh Quotes
          </button>
          <Link
            className={`btn ${list.length < 3 ? "pointer-events-none opacity-50" : ""}`}
            href={`/divergence?symbols=${encodeURIComponent(list.join(","))}`}
            title={list.length < 3 ? "Add at least 3 symbols to compare them against each other" : "Find which watchlist symbols are diverging from the rest"}
          >
            Divergence Scan
          </Link>
          <Link
            className={`btn ${list.length === 0 ? "pointer-events-none opacity-50" : ""}`}
            href="/screener"
            title="Use the Screener's 'Screen My Watchlist' button to filter these symbols by technicals"
          >
            Screen These
          </Link>
        </div>
        <div className="mt-3 flex flex-wrap gap-2">
          {list.length === 0 ? (
            <span className="text-sm text-[#9aaccc]">No symbols added yet.</span>
          ) : (
            list.map((s) => (
              <button key={s} className="badge" onClick={() => removeSymbol(s)} title="Remove from watchlist">
                {s} ×
              </button>
            ))
          )}
        </div>
      </PanelSection>

      {loading ? <CardNotice>Loading watchlist quotes...</CardNotice> : null}
      {error ? <CardNotice tone="danger">{error}</CardNotice> : null}

      <PanelSection>
        <PanelHeader title="Watchlist Quotes" badge={`${data?.count ?? 0} symbols`} />
        <div className="table-wrap">
          <table className="table">
            <thead>
              <tr>
                <th>Ticker</th>
                <th>Name</th>
                <th>Price</th>
                <th>% Move</th>
                <th>Volume</th>
              </tr>
            </thead>
            <tbody>
              {!data || data.quotes.length === 0 ? (
                <EmptyTableRow colSpan={5} message="Add symbols to start tracking." />
              ) : (
                data.quotes.map((q) => (
                  <tr key={q.symbol}>
                    <td>
                      <TickerBoardLink className="font-semibold text-[#5ec4ff]" symbol={q.symbol}>
                        {q.symbol}
                      </TickerBoardLink>
                    </td>
                    <td>{q.name}</td>
                    <td>{formatPrice(q.price)}</td>
                    <td className={pctColorClass(q.change_pct)}>
                      {formatPercent(q.change_pct)}
                    </td>
                    <td>{formatInteger(q.volume)}</td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>
      </PanelSection>
    </div>
  );
}
