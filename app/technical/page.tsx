"use client";

import { Suspense, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useSearchParams } from "next/navigation";
import { Line, LineChart, CartesianGrid, ReferenceDot, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { PriceVolumeChart } from "@/components/price-volume-chart";
import { TickerBoardLink } from "@/components/ticker-board-link";
import { apiGet } from "@/lib/api";

type TechnicalResponse = {
  symbol: string;
  range: string;
  updated_at: string;
  latest: {
    close: number;
    rsi14: number | null;
    sma20: number | null;
    sma50: number | null;
    sma200: number | null;
    macd: number | null;
    macd_signal: number | null;
    rvol: number | null;
  };
  flags: {
    rsi_overbought: boolean;
    rsi_oversold: boolean;
    above_sma50: boolean;
    above_sma200: boolean;
    bullish_crossover_50: boolean;
    bearish_crossover_50: boolean;
  };
  data: Array<{
    date: string;
    open: number;
    high: number;
    low: number;
    close: number;
    volume: number;
    rsi14: number | null;
    sma20: number | null;
    sma50: number | null;
    sma200: number | null;
    macd: number | null;
    macd_signal: number | null;
  }>;
  calculation_notes: Record<string, string>;
};

const RANGES = ["1M", "3M", "6M", "1Y", "5Y"] as const;
const RECENT_TECHNICALS_KEY = "openbb_recent_technical_symbols";
const MAX_RECENT = 12;

export default function TechnicalPage() {
  return (
    <Suspense fallback={<div className="card p-4 text-[#9aaccc]">Loading technical view...</div>}>
      <TechnicalPageInner />
    </Suspense>
  );
}

function TechnicalPageInner() {
  const searchParams = useSearchParams();
  const initialSymbol = (searchParams.get("symbol") || "AAPL").toUpperCase().trim() || "AAPL";
  const [symbol, setSymbol] = useState(initialSymbol);
  const [submitted, setSubmitted] = useState(initialSymbol);
  const [range, setRange] = useState<(typeof RANGES)[number]>("1Y");
  const [recentSymbols, setRecentSymbols] = useState<string[]>([]);
  const [data, setData] = useState<TechnicalResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const hasMountedSubmittedEffect = useRef(false);

  const run = useCallback(async (target: string, selectedRange: (typeof RANGES)[number]) => {
    const normalizedTarget = target.trim().toUpperCase();
    if (!normalizedTarget) return;
    setSubmitted(normalizedTarget);
    setLoading(true);
    setError(null);
    try {
      const res = await apiGet<TechnicalResponse>(`/api/ticker/${normalizedTarget}/technicals?range=${selectedRange}`);
      setData(res);
    } catch (e) {
      setError(String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  const badges = useMemo(() => {
    if (!data) return [];
    // MACD cross + unusual volume computed client-side from the series the API already returns.
    const rows = data.data;
    const last = rows[rows.length - 1];
    const prev = rows.length > 1 ? rows[rows.length - 2] : null;
    const macdBullCross =
      prev?.macd != null && prev?.macd_signal != null && last?.macd != null && last?.macd_signal != null
        ? prev.macd <= prev.macd_signal && last.macd > last.macd_signal
        : false;
    const macdBearCross =
      prev?.macd != null && prev?.macd_signal != null && last?.macd != null && last?.macd_signal != null
        ? prev.macd >= prev.macd_signal && last.macd < last.macd_signal
        : false;
    const unusualVolume = data.latest.rvol != null && data.latest.rvol >= 2;
    return [
      ["RSI Overbought", data.flags.rsi_overbought],
      ["RSI Oversold", data.flags.rsi_oversold],
      ["Above 50 SMA", data.flags.above_sma50],
      ["Above 200 SMA", data.flags.above_sma200],
      ["Bullish 50 Cross", data.flags.bullish_crossover_50],
      ["Bearish 50 Cross", data.flags.bearish_crossover_50],
      ["MACD Bull Cross", macdBullCross],
      ["MACD Bear Cross", macdBearCross],
      [`Unusual Volume${data.latest.rvol != null ? ` (RVOL ${data.latest.rvol.toFixed(1)})` : ""}`, unusualVolume],
    ] as const;
  }, [data]);

  const latestPoint = useMemo(() => {
    if (!data?.data?.length) return null;
    return data.data[data.data.length - 1];
  }, [data?.data]);

  const priceSignals = useMemo(() => {
    if (!data) return [];
    const out: Array<{ icon: string; text: string; tone: "up" | "down" | "neutral" }> = [];
    if (data.flags.bullish_crossover_50) out.push({ icon: "⬆", text: "Bullish 50 SMA Cross", tone: "up" });
    if (data.flags.bearish_crossover_50) out.push({ icon: "⬇", text: "Bearish 50 SMA Cross", tone: "down" });
    if (data.flags.rsi_overbought) out.push({ icon: "⚠", text: "RSI Overbought", tone: "down" });
    if (data.flags.rsi_oversold) out.push({ icon: "✳", text: "RSI Oversold", tone: "up" });
    if (!out.length) out.push({ icon: "•", text: "No active trigger", tone: "neutral" });
    return out;
  }, [data]);

  useEffect(() => {
    void run(submitted, range);
  }, [range, run, submitted]);

  useEffect(() => {
    const raw = localStorage.getItem(RECENT_TECHNICALS_KEY);
    if (!raw) return;
    try {
      const parsed = JSON.parse(raw) as string[];
      if (Array.isArray(parsed)) {
        setRecentSymbols(
          parsed
            .filter((s) => typeof s === "string" && /^[A-Z0-9.\-]{1,12}$/.test(s))
            .slice(0, MAX_RECENT)
        );
      }
    } catch {
      setRecentSymbols([]);
    }
  }, []);

  useEffect(() => {
    if (!hasMountedSubmittedEffect.current) {
      hasMountedSubmittedEffect.current = true;
      return;
    }
    const normalized = submitted.trim().toUpperCase();
    if (!normalized || !/^[A-Z0-9.\-]{1,12}$/.test(normalized)) return;
    setRecentSymbols((prev) => {
      const next = [normalized, ...prev.filter((s) => s !== normalized)].slice(0, MAX_RECENT);
      localStorage.setItem(RECENT_TECHNICALS_KEY, JSON.stringify(next));
      return next;
    });
  }, [submitted]);

  return (
    <div className="space-y-4">
      <div className="flex flex-col items-start justify-between gap-2 md:flex-row md:items-center">
        <div>
          <h1 className="text-2xl font-bold">Technical View</h1>
          <p className="text-sm text-[#9aaccc]">RSI(14), SMA(20/50/200), MACD and rule flags.</p>
        </div>
        <form
          className="flex flex-wrap items-center gap-2"
          onSubmit={(e) => {
            e.preventDefault();
            void run(symbol.trim().toUpperCase(), range);
          }}
        >
          <input
            className="input w-36"
            value={symbol}
            onChange={(e) => setSymbol(e.target.value.toUpperCase())}
            placeholder="Ticker"
          />
          {RANGES.map((r) => (
            <button type="button" key={r} className={`btn ${range === r ? "border-[#3a5ea8]" : ""}`} onClick={() => setRange(r)}>
              {r}
            </button>
          ))}
          <button type="submit" className="btn">
            Analyze
          </button>
          <TickerBoardLink className="btn" symbol={submitted}>Add to Chart Board</TickerBoardLink>
        </form>
      </div>

      <section className="card p-3">
        <div className="mb-2 flex items-center justify-between">
          <h2 className="text-sm font-semibold tracking-wide text-[#9aaccc] uppercase">Recently Searched</h2>
          <span className="badge">{recentSymbols.length} tickers</span>
        </div>
        {recentSymbols.length === 0 ? (
          <div className="text-sm text-[#9aaccc]">No recent tickers yet.</div>
        ) : (
          <div className="flex flex-wrap gap-2">
            {recentSymbols.map((s) => (
              <button
                key={s}
                className={`btn px-3 py-1.5 ${s === submitted ? "border-[#3a5ea8]" : ""}`}
                onClick={() => {
                  setSymbol(s);
                  void run(s, range);
                }}
              >
                {s}
              </button>
            ))}
          </div>
        )}
      </section>

      {loading ? <div className="card p-4 text-[#9aaccc]">Loading technical data...</div> : null}
      {error ? <div className="card p-4 text-[#ff6a6a]">{error}</div> : null}
      {!loading && !data ? (
        <div className="card p-4 text-[#9aaccc]">Enter a ticker and click Analyze.</div>
      ) : null}

      {data ? (
        <>
          <div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-6">
            {badges.map(([name, active]) => (
              <div key={name} className="card p-3">
                <div className="kpi-label">{name}</div>
                <div className={active ? "text-[#4fd18b] font-semibold" : "text-[#9aaccc]"}>{active ? "Yes" : "No"}</div>
              </div>
            ))}
          </div>

          <div className="relative">
            <PriceVolumeChart data={data.data} showSma interactive />
            <div className="pointer-events-none absolute top-3 right-3 flex max-w-[70%] flex-wrap justify-end gap-2">
              {priceSignals.map((s, idx) => (
                <span
                  key={`${s.text}-${idx}`}
                  className={`badge px-2 py-1 text-xs ${
                    s.tone === "up" ? "text-[#4fd18b]" : s.tone === "down" ? "text-[#ff6a6a]" : "text-[#9aaccc]"
                  }`}
                >
                  {s.icon} {s.text}
                </span>
              ))}
            </div>
          </div>

          <div className="grid gap-4 xl:grid-cols-2">
            <section className="card p-3">
              <h2 className="mb-2 text-sm font-semibold tracking-wide text-[#9aaccc] uppercase">
                RSI (14) {data.flags.rsi_overbought ? "⚠" : data.flags.rsi_oversold ? "✳" : "•"}
              </h2>
              <div className="h-[220px]">
                <ResponsiveContainer>
                  <LineChart data={data.data}>
                    <CartesianGrid stroke="#253353" strokeDasharray="3 3" />
                    <XAxis dataKey="date" tick={{ fill: "#9aaccc", fontSize: 12 }} minTickGap={24} />
                    <YAxis domain={[0, 100]} tick={{ fill: "#9aaccc", fontSize: 12 }} />
                    <Tooltip contentStyle={{ background: "#111a30", border: "1px solid #253353" }} />
                    <ReferenceLine y={70} stroke="#ff6a6a" strokeDasharray="4 4" />
                    <ReferenceLine y={30} stroke="#4fd18b" strokeDasharray="4 4" />
                    <Line type="monotone" dataKey="rsi14" stroke="#ffca6a" dot={false} strokeWidth={2} />
                    {latestPoint?.rsi14 != null && (data.flags.rsi_overbought || data.flags.rsi_oversold) ? (
                      <ReferenceDot
                        x={latestPoint.date}
                        y={latestPoint.rsi14}
                        r={5}
                        fill={data.flags.rsi_overbought ? "#ff6a6a" : "#4fd18b"}
                        stroke="#e7edf8"
                        strokeWidth={1}
                        label={{
                          value: data.flags.rsi_overbought ? "⚠" : "✳",
                          position: "top",
                          fill: "#e7edf8",
                          fontSize: 12,
                        }}
                      />
                    ) : null}
                  </LineChart>
                </ResponsiveContainer>
              </div>
            </section>

            <section className="card p-3">
              <h2 className="mb-2 text-sm font-semibold tracking-wide text-[#9aaccc] uppercase">
                MACD {data.flags.bullish_crossover_50 ? "⬆" : data.flags.bearish_crossover_50 ? "⬇" : "•"}
              </h2>
              <div className="h-[220px]">
                <ResponsiveContainer>
                  <LineChart data={data.data}>
                    <CartesianGrid stroke="#253353" strokeDasharray="3 3" />
                    <XAxis dataKey="date" tick={{ fill: "#9aaccc", fontSize: 12 }} minTickGap={24} />
                    <YAxis tick={{ fill: "#9aaccc", fontSize: 12 }} />
                    <Tooltip contentStyle={{ background: "#111a30", border: "1px solid #253353" }} />
                    <ReferenceLine y={0} stroke="#9aaccc" strokeDasharray="4 4" />
                    <Line type="monotone" dataKey="macd" stroke="#5ec4ff" dot={false} strokeWidth={2} />
                    <Line type="monotone" dataKey="macd_signal" stroke="#ff6a6a" dot={false} strokeWidth={2} />
                    {latestPoint?.macd != null && (data.flags.bullish_crossover_50 || data.flags.bearish_crossover_50) ? (
                      <ReferenceDot
                        x={latestPoint.date}
                        y={latestPoint.macd}
                        r={5}
                        fill={data.flags.bullish_crossover_50 ? "#4fd18b" : "#ff6a6a"}
                        stroke="#e7edf8"
                        strokeWidth={1}
                        label={{
                          value: data.flags.bullish_crossover_50 ? "⬆" : "⬇",
                          position: "top",
                          fill: "#e7edf8",
                          fontSize: 12,
                        }}
                      />
                    ) : null}
                  </LineChart>
                </ResponsiveContainer>
              </div>
            </section>
          </div>

          <section className="card p-3">
            <h2 className="mb-2 text-sm font-semibold tracking-wide text-[#9aaccc] uppercase">Calculation Notes</h2>
            <ul className="space-y-1 text-sm text-[#9aaccc]">
              {Object.entries(data.calculation_notes).map(([key, note]) => (
                <li key={key}>
                  <span className="font-semibold text-[#d9e6ff]">{key}:</span> {note}
                </li>
              ))}
            </ul>
          </section>
        </>
      ) : null}
    </div>
  );
}
