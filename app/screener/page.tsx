"use client";

import { useMemo, useState } from "react";
import { apiGet, apiPost } from "@/lib/api";
import { TickerBoardLink } from "@/components/ticker-board-link";

type ScreenerRow = {
  symbol: string;
  name: string;
  price: number | null;
  volume: number | null;
  percent_change: number | null;
  rsi14: number | null;
  sma20: number | null;
  sma50: number | null;
  sma200: number | null;
  above_sma20: boolean;
  above_sma50: boolean;
  above_sma200: boolean;
  rvol: number | null;
};

type ScreenerResponse = {
  rows: ScreenerRow[];
  count: number;
  updated_at: string;
  universe_count: number;
  provider?: string;
  source_providers?: string[];
};

type VolumeTriggerRow = {
  symbol: string;
  name: string;
  price: number | null;
  volume: number | null;
  percent_change: number | null;
  rsi14: number | null;
  rvol: number | null;
  has_triggers: boolean;
  trigger_count: number;
  triggers: string[];
  history_available?: boolean;
  analysis_error?: string | null;
};

type VolumeTriggerResponse = {
  rows: VolumeTriggerRow[];
  count: number;
  updated_at: string;
  universe_count: number;
  checked_count: number;
  analyzed_count?: number;
  history_failures?: number;
  history_failure_symbols?: string[];
  provider?: string;
  source_providers?: string[];
};

type TriBool = "any" | "yes" | "no";
type PresetFilters = {
  symbols?: string[];
  ticker_contains?: string;
  min_price?: number;
  max_price?: number;
  min_volume?: number;
  min_percent_change?: number;
  max_percent_change?: number;
  rsi_min?: number;
  rsi_max?: number;
  above_sma20?: boolean;
  above_sma50?: boolean;
  above_sma200?: boolean;
  limit?: number;
};

type ScreenerPreset = {
  id: string;
  label: string;
  description: string;
  filters: PresetFilters;
};

type ScreenerPayload = {
  symbols?: string[];
  ticker_contains?: string;
  min_price?: number;
  max_price?: number;
  min_volume?: number;
  min_percent_change?: number;
  max_percent_change?: number;
  rsi_min?: number;
  rsi_max?: number;
  above_sma20?: boolean;
  above_sma50?: boolean;
  above_sma200?: boolean;
  limit: number;
};

const PRESETS: ScreenerPreset[] = [
  {
    id: "momentum",
    label: "Momentum Breakouts",
    description: "Strong movers with volume confirmation and trend support.",
    filters: { min_percent_change: 1.5, min_volume: 1_000_000, above_sma20: true, above_sma50: true, rsi_min: 50, rsi_max: 82 },
  },
  {
    id: "oversold",
    label: "Oversold Bounce",
    description: "Potential mean-reversion candidates with weak RSI.",
    filters: { rsi_max: 35, min_volume: 500_000, max_percent_change: 1 },
  },
  {
    id: "trend",
    label: "Strong Uptrend",
    description: "Price above 20/50/200 with healthy RSI.",
    filters: { above_sma20: true, above_sma50: true, above_sma200: true, rsi_min: 50, rsi_max: 75, min_price: 10 },
  },
  {
    id: "high-volume",
    label: "High Volume Movers",
    description: "Large absolute volume and daily move.",
    filters: { min_volume: 1_500_000, min_percent_change: 1, min_price: 5 },
  },
  {
    id: "semis",
    label: "Semiconductors",
    description: "The real semiconductor peer list (same set the Divergence tab uses).",
    filters: {
      symbols: ["NVDA", "AMD", "AVGO", "TSM", "INTC", "MU", "QCOM", "TXN", "MRVL", "ON", "AMAT", "ASML", "SMH"],
    },
  },
  {
    id: "utilities",
    label: "Utilities Style",
    description: "Lower-volatility profile with moderate RSI and trend check.",
    filters: { rsi_min: 40, rsi_max: 65, above_sma50: true, min_price: 10, min_volume: 250_000 },
  },
];

function triToBool(v: TriBool): boolean | undefined {
  if (v === "yes") return true;
  if (v === "no") return false;
  return undefined;
}

function boolToTri(v?: boolean): TriBool {
  if (v === true) return "yes";
  if (v === false) return "no";
  return "any";
}

function clamp(value: number, min: number, max: number) {
  return Math.min(max, Math.max(min, value));
}

function asFiniteNumber(value: unknown): number | null {
  const n = typeof value === "number" ? value : Number(value);
  return Number.isFinite(n) ? n : null;
}

function fmtFixed(value: unknown, digits = 2) {
  const n = asFiniteNumber(value);
  return n == null ? "-" : n.toFixed(digits);
}

function fmtInt(value: unknown) {
  const n = asFiniteNumber(value);
  return n == null ? "-" : Intl.NumberFormat().format(n);
}

function triggerToneClass(trigger: string) {
  const value = trigger.toLowerCase();
  if (value.includes("bull")) return "text-[#4fd18b]";
  if (value.includes("bear")) return "text-[#ff6a6a]";
  return "text-[#9aaccc]";
}

export default function ScreenerPage() {
  const [tickerContains, setTickerContains] = useState("");
  const [minPrice, setMinPrice] = useState("");
  const [maxPrice, setMaxPrice] = useState("");
  const [minVolume, setMinVolume] = useState("");
  const [minPct, setMinPct] = useState("");
  const [maxPct, setMaxPct] = useState("");
  const [rsiMin, setRsiMin] = useState("");
  const [rsiMax, setRsiMax] = useState("");
  const [above20, setAbove20] = useState<TriBool>("any");
  const [above50, setAbove50] = useState<TriBool>("any");
  const [above200, setAbove200] = useState<TriBool>("any");

  const [data, setData] = useState<ScreenerResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [triggerLoading, setTriggerLoading] = useState(false);
  const [triggerError, setTriggerError] = useState<string | null>(null);
  const [triggerData, setTriggerData] = useState<VolumeTriggerResponse | null>(null);
  const [activePreset, setActivePreset] = useState<string | null>(null);
  const [tableZoom, setTableZoom] = useState(100);
  const [sortKey, setSortKey] = useState<keyof ScreenerRow>("percent_change");
  const [sortDesc, setSortDesc] = useState(true);

  const payload: ScreenerPayload = useMemo(
    () => ({
      ticker_contains: tickerContains || undefined,
      min_price: minPrice ? Number(minPrice) : undefined,
      max_price: maxPrice ? Number(maxPrice) : undefined,
      min_volume: minVolume ? Number(minVolume) : undefined,
      min_percent_change: minPct ? Number(minPct) : undefined,
      max_percent_change: maxPct ? Number(maxPct) : undefined,
      rsi_min: rsiMin ? Number(rsiMin) : undefined,
      rsi_max: rsiMax ? Number(rsiMax) : undefined,
      above_sma20: triToBool(above20),
      above_sma50: triToBool(above50),
      above_sma200: triToBool(above200),
      limit: 150,
    }),
    [tickerContains, minPrice, maxPrice, minVolume, minPct, maxPct, rsiMin, rsiMax, above20, above50, above200]
  );

  const runWithPayload = async (request: ScreenerPayload) => {
    setLoading(true);
    setError(null);
    try {
      const res = await apiPost<ScreenerResponse>("/api/screener", request);
      setData(res);
    } catch (e) {
      setError(String(e));
    } finally {
      setLoading(false);
    }
  };

  const run = async () => runWithPayload(payload);

  const runTopVolumeTriggers = async () => {
    setTriggerLoading(true);
    setTriggerError(null);
    try {
      const res = await apiGet<VolumeTriggerResponse>("/api/screener/top-volume-triggers?limit=30&universe_size=80");
      setTriggerData(res);
    } catch (e) {
      setTriggerError(String(e));
    } finally {
      setTriggerLoading(false);
    }
  };

  const applyPreset = async (preset: ScreenerPreset) => {
    const f = preset.filters;
    setActivePreset(preset.id);
    setTickerContains(f.ticker_contains ?? "");
    setMinPrice(f.min_price == null ? "" : String(f.min_price));
    setMaxPrice(f.max_price == null ? "" : String(f.max_price));
    setMinVolume(f.min_volume == null ? "" : String(f.min_volume));
    setMinPct(f.min_percent_change == null ? "" : String(f.min_percent_change));
    setMaxPct(f.max_percent_change == null ? "" : String(f.max_percent_change));
    setRsiMin(f.rsi_min == null ? "" : String(f.rsi_min));
    setRsiMax(f.rsi_max == null ? "" : String(f.rsi_max));
    setAbove20(boolToTri(f.above_sma20));
    setAbove50(boolToTri(f.above_sma50));
    setAbove200(boolToTri(f.above_sma200));

    await runWithPayload({
      ...f,
      limit: f.limit ?? 150,
    });
  };

  const screenWatchlist = async () => {
    let watchlist: string[] = [];
    try {
      const raw = localStorage.getItem("openbb_watchlist_symbols");
      const parsed = raw ? (JSON.parse(raw) as string[]) : [];
      if (Array.isArray(parsed)) {
        watchlist = parsed.filter((s) => typeof s === "string" && /^[A-Z0-9.\-]{1,12}$/.test(s));
      }
    } catch {
      watchlist = [];
    }
    if (!watchlist.length) {
      setError("Your watchlist is empty — add symbols on the Watchlist tab first.");
      return;
    }
    setActivePreset("watchlist");
    await runWithPayload({ ...payload, symbols: watchlist });
  };

  const sortedRows = useMemo(() => {
    const rows = data?.rows ?? [];
    return [...rows].sort((a, b) => {
      const av = a[sortKey];
      const bv = b[sortKey];
      if (typeof av === "string" || typeof bv === "string") {
        return sortDesc ? String(bv ?? "").localeCompare(String(av ?? "")) : String(av ?? "").localeCompare(String(bv ?? ""));
      }
      const an = asFiniteNumber(av) ?? Number.NEGATIVE_INFINITY;
      const bn = asFiniteNumber(bv) ?? Number.NEGATIVE_INFINITY;
      return sortDesc ? bn - an : an - bn;
    });
  }, [data?.rows, sortKey, sortDesc]);

  const onSort = (key: keyof ScreenerRow) => {
    if (key === sortKey) setSortDesc((v) => !v);
    else {
      setSortKey(key);
      setSortDesc(true);
    }
  };

  const sortIndicator = (key: keyof ScreenerRow) => (sortKey === key ? (sortDesc ? " ↓" : " ↑") : "");

  const clearFilters = () => {
    setActivePreset(null);
    setTickerContains("");
    setMinPrice("");
    setMaxPrice("");
    setMinVolume("");
    setMinPct("");
    setMaxPct("");
    setRsiMin("");
    setRsiMax("");
    setAbove20("any");
    setAbove50("any");
    setAbove200("any");
  };

  const adjustZoom = (delta: number) => {
    setTableZoom((z) => clamp(z + delta, 70, 170));
  };

  return (
    <div className="space-y-4">
      <div>
        <h1 className="text-2xl font-bold">Screener</h1>
        <p className="text-sm text-[#9aaccc]">Server-side filtered universe based on OpenBB discovery + computed technicals.</p>
      </div>

      <section className="card p-3">
        <div className="mb-3 flex flex-wrap items-center gap-2">
          {PRESETS.map((preset) => (
            <button
              key={preset.id}
              className={`btn ${activePreset === preset.id ? "border-[#3a5ea8]" : ""}`}
              title={preset.description}
              onClick={() => void applyPreset(preset)}
            >
              {preset.label}
            </button>
          ))}
          <button
            className={`btn ${activePreset === "watchlist" ? "border-[#3a5ea8]" : ""}`}
            title="Run the current filters against your Watchlist symbols instead of the default universe."
            onClick={() => void screenWatchlist()}
          >
            Screen My Watchlist
          </button>
          <button className="btn" onClick={clearFilters}>
            Clear
          </button>
          <button className="btn" onClick={() => void runTopVolumeTriggers()}>
            Top Volume Trigger Scan
          </button>
        </div>
        <div className="grid gap-2 md:grid-cols-4 xl:grid-cols-6">
          <input className="input" placeholder="Ticker contains" value={tickerContains} onChange={(e) => setTickerContains(e.target.value.toUpperCase())} />
          <input className="input" placeholder="Min price" value={minPrice} onChange={(e) => setMinPrice(e.target.value)} />
          <input className="input" placeholder="Max price" value={maxPrice} onChange={(e) => setMaxPrice(e.target.value)} />
          <input className="input" placeholder="Min volume" value={minVolume} onChange={(e) => setMinVolume(e.target.value)} />
          <input className="input" placeholder="Min % change" value={minPct} onChange={(e) => setMinPct(e.target.value)} />
          <input className="input" placeholder="Max % change" value={maxPct} onChange={(e) => setMaxPct(e.target.value)} />
          <input className="input" placeholder="RSI min" value={rsiMin} onChange={(e) => setRsiMin(e.target.value)} />
          <input className="input" placeholder="RSI max" value={rsiMax} onChange={(e) => setRsiMax(e.target.value)} />
          <select className="input" value={above20} onChange={(e) => setAbove20(e.target.value as TriBool)}>
            <option value="any">Above SMA20: Any</option>
            <option value="yes">Above SMA20: Yes</option>
            <option value="no">Above SMA20: No</option>
          </select>
          <select className="input" value={above50} onChange={(e) => setAbove50(e.target.value as TriBool)}>
            <option value="any">Above SMA50: Any</option>
            <option value="yes">Above SMA50: Yes</option>
            <option value="no">Above SMA50: No</option>
          </select>
          <select className="input" value={above200} onChange={(e) => setAbove200(e.target.value as TriBool)}>
            <option value="any">Above SMA200: Any</option>
            <option value="yes">Above SMA200: Yes</option>
            <option value="no">Above SMA200: No</option>
          </select>
          <button className="btn" onClick={run}>Run Screener</button>
        </div>
      </section>

      {loading ? <div className="card p-4 text-[#9aaccc]">Screening...</div> : null}
      {error ? <div className="card p-4 text-[#ff6a6a]">{error}</div> : null}
      {triggerLoading ? <div className="card p-4 text-[#9aaccc]">Scanning top-volume triggers...</div> : null}
      {triggerError ? <div className="card p-4 text-[#ff6a6a]">{triggerError}</div> : null}

      {triggerData ? (
        <section className="card p-3">
          <div className="mb-2 flex items-center justify-between">
            <h2 className="text-sm font-semibold tracking-wide text-[#9aaccc] uppercase">Top Volume Trigger Scan</h2>
            <div className="text-sm text-[#9aaccc]">
              {triggerData.count} rows | checked {triggerData.checked_count} / universe {triggerData.universe_count} | analyzed{" "}
              {triggerData.analyzed_count ?? "-"} | history fail {triggerData.history_failures ?? "-"} |{" "}
              {new Date(triggerData.updated_at).toLocaleString()}
            </div>
          </div>
          {triggerData.history_failure_symbols?.length ? (
            <div className="mb-2 text-xs text-[#ffb46a]">
              History unavailable for: {triggerData.history_failure_symbols.join(", ")}
            </div>
          ) : null}
          <div className="mb-2 text-xs text-[#9aaccc]">
            Data source: {triggerData.source_providers?.length ? triggerData.source_providers.join(", ") : triggerData.provider || "router"}
          </div>
          <div className="table-wrap">
            <table className="table">
              <thead>
                <tr>
                  <th>Ticker</th>
                  <th>Name</th>
                  <th>Price</th>
                  <th>% Chg</th>
                  <th>Volume</th>
                  <th>RSI</th>
                  <th>RVOL</th>
                  <th>Triggers</th>
                </tr>
              </thead>
              <tbody>
                {triggerData.rows.length === 0 ? (
                  <tr>
                    <td colSpan={8} className="text-[#9aaccc]">
                      No top-volume rows were returned.
                    </td>
                  </tr>
                ) : (
                  triggerData.rows.map((row) => (
                    <tr key={row.symbol}>
                      <td>
                        <TickerBoardLink className="font-semibold text-[#5ec4ff]" symbol={row.symbol}>
                          {row.symbol}
                        </TickerBoardLink>
                      </td>
                      <td className="max-w-[220px] truncate">{row.name}</td>
                      <td>{fmtFixed(row.price, 2)}</td>
                      <td className={(asFiniteNumber(row.percent_change) ?? 0) >= 0 ? "text-[#4fd18b]" : "text-[#ff6a6a]"}>
                        {asFiniteNumber(row.percent_change) == null ? "-" : `${fmtFixed(row.percent_change, 2)}%`}
                      </td>
                      <td>{fmtInt(row.volume)}</td>
                      <td>{fmtFixed(row.rsi14, 2)}</td>
                      <td>{fmtFixed(row.rvol, 2)}</td>
                      <td className={!row.history_available ? "text-[#ffb46a]" : "text-[#9aaccc]"} title={row.analysis_error || ""}>
                        {!row.history_available ? (
                          "History unavailable"
                        ) : row.trigger_count > 0 ? (
                          row.triggers.map((trigger, idx) => (
                            <span key={`${row.symbol}-${trigger}-${idx}`} className={triggerToneClass(trigger)}>
                              {idx > 0 ? ", " : ""}
                              {trigger}
                            </span>
                          ))
                        ) : (
                          "No active trigger"
                        )}
                      </td>
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
            <h2 className="text-sm font-semibold tracking-wide text-[#9aaccc] uppercase">Results</h2>
            <div className="flex items-center gap-2 text-sm text-[#9aaccc]">
              <span>{data.count} matched / {data.universe_count} universe | {new Date(data.updated_at).toLocaleString()}</span>
              <span className="badge">Zoom {tableZoom}%</span>
              <button className="btn px-2 py-1" onClick={() => adjustZoom(-5)}>-</button>
              <button className="btn px-2 py-1" onClick={() => setTableZoom(100)}>Reset</button>
              <button className="btn px-2 py-1" onClick={() => adjustZoom(5)}>+</button>
            </div>
          </div>
          <div className="mb-2 text-xs text-[#9aaccc]">
            Data source: {data.source_providers?.length ? data.source_providers.join(", ") : data.provider || "router"}
          </div>
          <div className="mb-2 text-xs text-[#9aaccc]">Tip: hold Ctrl (or Cmd on Mac) and scroll mouse wheel over this table to zoom.</div>
          <div
            className="table-wrap"
            onWheel={(e) => {
              if (!e.ctrlKey && !e.metaKey) return;
              e.preventDefault();
              adjustZoom(e.deltaY > 0 ? -5 : 5);
            }}
          >
            <table className="table" style={{ fontSize: `${tableZoom}%` }}>
              <thead>
                <tr>
                  <th>
                    <button className="cursor-pointer" onClick={() => onSort("symbol")}>Ticker{sortIndicator("symbol")}</button>
                  </th>
                  <th>Name</th>
                  <th>
                    <button className="cursor-pointer" onClick={() => onSort("price")}>Price{sortIndicator("price")}</button>
                  </th>
                  <th>
                    <button className="cursor-pointer" onClick={() => onSort("percent_change")}>% Chg{sortIndicator("percent_change")}</button>
                  </th>
                  <th>
                    <button className="cursor-pointer" onClick={() => onSort("volume")}>Volume{sortIndicator("volume")}</button>
                  </th>
                  <th>
                    <button className="cursor-pointer" onClick={() => onSort("rsi14")}>RSI{sortIndicator("rsi14")}</button>
                  </th>
                  <th>
                    <button className="cursor-pointer" onClick={() => onSort("rvol")}>RVOL{sortIndicator("rvol")}</button>
                  </th>
                  <th>Above 20</th>
                  <th>Above 50</th>
                  <th>Above 200</th>
                </tr>
              </thead>
              <tbody>
                {sortedRows.length === 0 ? (
                  <tr>
                    <td colSpan={10} className="text-[#9aaccc]">
                      No rows matched the current filters.
                    </td>
                  </tr>
                ) : (
                  sortedRows.map((row) => (
                    <tr key={row.symbol}>
                      <td>
                        <TickerBoardLink className="font-semibold text-[#5ec4ff]" symbol={row.symbol}>
                          {row.symbol}
                        </TickerBoardLink>
                      </td>
                      <td className="max-w-[220px] truncate">{row.name}</td>
                      <td>{fmtFixed(row.price, 2)}</td>
                      <td className={(asFiniteNumber(row.percent_change) ?? 0) >= 0 ? "text-[#4fd18b]" : "text-[#ff6a6a]"}>
                        {asFiniteNumber(row.percent_change) == null ? "-" : `${fmtFixed(row.percent_change, 2)}%`}
                      </td>
                      <td>{fmtInt(row.volume)}</td>
                      <td>{fmtFixed(row.rsi14, 2)}</td>
                      <td>{fmtFixed(row.rvol, 2)}</td>
                      <td>{row.above_sma20 ? "Yes" : "No"}</td>
                      <td>{row.above_sma50 ? "Yes" : "No"}</td>
                      <td>{row.above_sma200 ? "Yes" : "No"}</td>
                    </tr>
                  ))
                )}
              </tbody>
            </table>
          </div>
        </section>
      ) : (
        <div className="card p-4 text-[#9aaccc]">Run the screener to see results.</div>
      )}
    </div>
  );
}
