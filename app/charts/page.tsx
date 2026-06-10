"use client";

import Link from "next/link";
import { Suspense, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useSearchParams } from "next/navigation";
import { apiGet } from "@/lib/api";

type ChartPoint = {
  date: string;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
};

type HistoricalResponse = {
  symbol: string;
  range: string;
  updated_at: string;
  rows: number;
  data: ChartPoint[];
};

type ChartCardProps = {
  symbol: string;
  range: "1M" | "3M" | "6M" | "1Y" | "5Y";
  onRemove: (symbol: string) => void;
};

const STORAGE_KEY = "openbb_chart_board_symbols";
const MAX_CHARTS = 9;
const RANGES = ["1M", "3M", "6M", "1Y", "5Y"] as const;

function normalizeSymbolList(value: string): string[] {
  return value
    .split(",")
    .map((s) => s.trim().toUpperCase())
    .filter((s) => /^[A-Z0-9.\-]{1,12}$/.test(s));
}

function addSymbols(base: string[], incoming: string[]): string[] {
  const next = [...base];
  for (const symbol of incoming) {
    if (!next.includes(symbol)) next.push(symbol);
  }
  if (next.length > MAX_CHARTS) {
    return next.slice(next.length - MAX_CHARTS);
  }
  return next;
}

function clamp(value: number, min: number, max: number) {
  return Math.min(max, Math.max(min, value));
}

function MiniCandlestick({ data, yPanPct = 0 }: { data: ChartPoint[]; yPanPct?: number }) {
  const containerRef = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(0);
  const [hoverY, setHoverY] = useState<number | null>(null);
  const height = 260;

  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;
    const obs = new ResizeObserver(([entry]) => {
      setWidth(Math.floor(entry.contentRect.width));
    });
    obs.observe(el);
    setWidth(Math.floor(el.clientWidth));
    return () => obs.disconnect();
  }, []);

  const chart = useMemo(() => {
    if (!data.length || width < 40) return null;

    const pad = { top: 10, right: 48, bottom: 20, left: 6 };
    const volLane = 34; // volume bars live in a lane between the candles and the x labels
    const plotW = Math.max(1, width - pad.left - pad.right);
    const plotH = Math.max(1, height - pad.top - pad.bottom - volLane);

    const rawMin = Math.min(...data.map((d) => d.low));
    const rawMax = Math.max(...data.map((d) => d.high));
    const rawSpan = Math.max(1e-9, rawMax - rawMin);
    const shift = rawSpan * (yPanPct / 100);
    const min = rawMin + shift;
    const max = rawMax + shift;
    const span = Math.max(1e-9, max - min);
    const toY = (value: number) => pad.top + ((max - value) / span) * plotH;

    const maxVolume = Math.max(1, ...data.map((d) => d.volume || 0));
    const volTop = height - pad.bottom - volLane;
    const step = plotW / data.length;
    const candleW = Math.max(2, Math.min(11, step * 0.62));
    const gridTicks = 5;
    const xStride = Math.max(1, Math.ceil(data.length / 6));

    return { pad, plotW, plotH, min, max, toY, step, candleW, gridTicks, xStride, maxVolume, volLane, volTop };
  }, [data, width, yPanPct]);

  return (
    <div ref={containerRef} className="h-[260px] w-full">
      {chart ? (
        <svg
          width={width}
          height={height}
          role="img"
          aria-label="Candlestick chart"
          onMouseMove={(e) => {
            const rect = e.currentTarget.getBoundingClientRect();
            const y = e.clientY - rect.top;
            setHoverY(clamp(y, chart.pad.top, chart.volTop - 2));
          }}
          onMouseLeave={() => setHoverY(null)}
        >
          {[...Array(chart.gridTicks)].map((_, i) => {
            const t = i / (chart.gridTicks - 1);
            const y = chart.pad.top + chart.plotH * t;
            const value = chart.max - (chart.max - chart.min) * t;
            return (
              <g key={`grid-${i}`}>
                <line x1={chart.pad.left} y1={y} x2={width - chart.pad.right} y2={y} stroke="#253353" strokeDasharray="3 3" />
                <text x={width - chart.pad.right + 6} y={y + 4} fill="#9aaccc" fontSize="10">
                  {value.toFixed(2)}
                </text>
              </g>
            );
          })}

          {hoverY != null ? (
            <g>
              <line
                x1={chart.pad.left}
                y1={hoverY}
                x2={width - chart.pad.right}
                y2={hoverY}
                stroke="#9fb7de"
                strokeDasharray="5 4"
                strokeWidth={1}
              />
              <rect x={width - chart.pad.right + 4} y={hoverY - 9} width={44} height={16} rx={3} fill="#0f1930" opacity={0.92} />
              <text x={width - chart.pad.right + 26} y={hoverY + 2} fill="#dce8ff" fontSize="10" textAnchor="middle">
                {(chart.max - ((hoverY - chart.pad.top) / chart.plotH) * (chart.max - chart.min)).toFixed(2)}
              </text>
            </g>
          ) : null}

          {data.map((row, i) => {
            const x = chart.pad.left + chart.step * (i + 0.5);
            const yHigh = chart.toY(row.high);
            const yLow = chart.toY(row.low);
            const yOpen = chart.toY(row.open);
            const yClose = chart.toY(row.close);
            const top = Math.min(yOpen, yClose);
            const bodyH = Math.max(1.2, Math.abs(yClose - yOpen));
            const up = row.close >= row.open;
            const color = up ? "#4fd18b" : "#ff6a6a";
            const volH = Math.max(1, ((row.volume || 0) / chart.maxVolume) * (chart.volLane - 4));
            return (
              <g key={row.date}>
                <line x1={x} y1={yHigh} x2={x} y2={yLow} stroke={color} strokeWidth={1} />
                <rect x={x - chart.candleW / 2} y={top} width={chart.candleW} height={bodyH} fill={color} opacity={0.9} rx={1} />
                <rect
                  x={x - chart.candleW / 2}
                  y={chart.volTop + (chart.volLane - volH)}
                  width={chart.candleW}
                  height={volH}
                  fill={color}
                  opacity={0.35}
                />
                {(i % chart.xStride === 0 || i === data.length - 1) && (
                  <text x={x} y={height - 5} fill="#9aaccc" fontSize="10" textAnchor="middle">
                    {row.date.slice(5)}
                  </text>
                )}
              </g>
            );
          })}
        </svg>
      ) : null}
    </div>
  );
}

function ChartCard({ symbol, range, onRemove }: ChartCardProps) {
  const [data, setData] = useState<HistoricalResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [zoomPct, setZoomPct] = useState(100);
  const [offset, setOffset] = useState(0);
  const [yPanPct, setYPanPct] = useState(0);
  const dragRef = useRef<{
    active: boolean;
    pointerId: number | null;
    startX: number;
    startY: number;
    startOffset: number;
    startYPan: number;
  }>({
    active: false,
    pointerId: null,
    startX: 0,
    startY: 0,
    startOffset: 0,
    startYPan: 0,
  });

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await apiGet<HistoricalResponse>(`/api/ticker/${encodeURIComponent(symbol)}/historical?range=${encodeURIComponent(range)}`);
      setData(res);
    } catch (e) {
      setError(String(e));
    } finally {
      setLoading(false);
    }
  }, [range, symbol]);

  useEffect(() => {
    void load();
  }, [load]);

  const totalRows = data?.data.length ?? 0;
  const visibleCount = useMemo(() => clamp(Math.round(totalRows * (zoomPct / 100)), 20, totalRows || 20), [totalRows, zoomPct]);
  const maxOffset = useMemo(() => Math.max(0, totalRows - visibleCount), [totalRows, visibleCount]);

  const visibleData = useMemo(() => {
    const rows = data?.data ?? [];
    if (!rows.length) return rows;
    const boundedOffset = clamp(offset, 0, maxOffset);
    const start = rows.length - visibleCount - boundedOffset;
    const end = start + visibleCount;
    return rows.slice(start, end);
  }, [data?.data, offset, visibleCount, maxOffset]);

  const onZoom = (deltaPct: number) => {
    setZoomPct((v) => clamp(v + deltaPct, 20, 100));
  };

  const panStep = useMemo(() => Math.max(5, Math.floor((visibleData.length || 20) / 4)), [visibleData.length]);

  const onPan = (delta: number) => {
    setOffset((v) => clamp(v + delta, 0, maxOffset));
  };

  const rangeStats = useMemo(() => {
    if (!visibleData.length) return null;
    const first = visibleData[0].close;
    const last = visibleData[visibleData.length - 1].close;
    const changePct = first ? ((last / first - 1) * 100) : null;
    return { last, changePct };
  }, [visibleData]);

  return (
    <section className="card p-3">
      <div className="mb-2 flex items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <div className="text-lg font-semibold">{symbol}</div>
          {rangeStats ? (
            <>
              <span className="text-sm font-semibold">{rangeStats.last.toFixed(2)}</span>
              {rangeStats.changePct != null ? (
                <span className={`text-sm ${rangeStats.changePct >= 0 ? "text-[#4fd18b]" : "text-[#ff6a6a]"}`}>
                  {rangeStats.changePct > 0 ? "+" : ""}
                  {rangeStats.changePct.toFixed(2)}%
                </span>
              ) : null}
            </>
          ) : null}
          <span className="badge">{range}</span>
          <span className="badge">Zoom {zoomPct}%</span>
        </div>
        <div className="flex items-center gap-2">
          <Link className="btn px-2 py-1" href={`/ticker/${symbol}`} title="Open ticker detail">
            Detail
          </Link>
          <button className="btn px-2 py-1" onClick={() => onRemove(symbol)} title="Remove this chart">
            Remove
          </button>
        </div>
      </div>

      {loading ? <div className="text-sm text-[#9aaccc]">Loading chart...</div> : null}
      {error ? <div className="text-sm text-[#ff6a6a]">{error}</div> : null}
      {!loading && !error && data ? (
        <div
          className="touch-none select-none"
          onWheel={(e) => {
            e.preventDefault();
            if (e.shiftKey) {
              onPan(e.deltaY > 0 ? panStep : -panStep);
            } else {
              onZoom(e.deltaY > 0 ? 10 : -10);
            }
          }}
          onPointerDown={(e) => {
            dragRef.current = {
              active: true,
              pointerId: e.pointerId,
              startX: e.clientX,
              startY: e.clientY,
              startOffset: offset,
              startYPan: yPanPct,
            };
            (e.currentTarget as HTMLDivElement).setPointerCapture(e.pointerId);
          }}
          onPointerMove={(e) => {
            const drag = dragRef.current;
            if (!drag.active || drag.pointerId !== e.pointerId) return;
            const dx = e.clientX - drag.startX;
            const dy = e.clientY - drag.startY;
            const dragOffset = Math.round(dx / 6);
            const dragYPan = dy / 4;
            setOffset(clamp(drag.startOffset + dragOffset, 0, maxOffset));
            setYPanPct(clamp(drag.startYPan + dragYPan, -80, 80));
          }}
          onPointerUp={(e) => {
            const drag = dragRef.current;
            if (drag.pointerId === e.pointerId) {
              dragRef.current.active = false;
              dragRef.current.pointerId = null;
            }
          }}
          onPointerCancel={(e) => {
            const drag = dragRef.current;
            if (drag.pointerId === e.pointerId) {
              dragRef.current.active = false;
              dragRef.current.pointerId = null;
            }
          }}
          onDoubleClick={() => {
            setZoomPct(100);
            setOffset(0);
            setYPanPct(0);
          }}
        >
          <MiniCandlestick data={visibleData} yPanPct={yPanPct} />
        </div>
      ) : null}
      {!loading && !error && data ? (
        <div className="mt-2 text-xs text-[#9aaccc]">
          Drag to pan left/right/up/down. Scroll to zoom. Hold Shift + scroll to pan horizontally. Double-click to reset view.
        </div>
      ) : null}
    </section>
  );
}

export default function ChartsPage() {
  return (
    <Suspense fallback={<div className="card p-4 text-[#9aaccc]">Loading chart board...</div>}>
      <ChartsPageInner />
    </Suspense>
  );
}

function ChartsPageInner() {
  const searchParams = useSearchParams();
  const addParam = searchParams.get("add") || "";
  const [symbols, setSymbols] = useState<string[]>([]);
  const [hydrated, setHydrated] = useState(false);
  const [range, setRange] = useState<(typeof RANGES)[number]>("1Y");

  useEffect(() => {
    let fromStorage: string[] = [];
    const raw = localStorage.getItem(STORAGE_KEY);
    if (raw) {
      try {
        const parsed = JSON.parse(raw) as string[];
        if (Array.isArray(parsed)) {
          fromStorage = normalizeSymbolList(parsed.filter((s) => typeof s === "string").join(",")).slice(-MAX_CHARTS);
        }
      } catch {
        fromStorage = [];
      }
    }

    const frame = window.requestAnimationFrame(() => {
      setSymbols(fromStorage);
      setHydrated(true);
    });
    return () => window.cancelAnimationFrame(frame);
  }, []);

  useEffect(() => {
    if (!hydrated) return;
    const incoming = normalizeSymbolList(addParam);
    if (!incoming.length) return;
    const frame = window.requestAnimationFrame(() => {
      setSymbols((prev) => addSymbols(prev, incoming));
      window.history.replaceState({}, "", "/charts");
    });
    return () => window.cancelAnimationFrame(frame);
  }, [addParam, hydrated]);

  useEffect(() => {
    if (!hydrated) return;
    localStorage.setItem(STORAGE_KEY, JSON.stringify(symbols));
  }, [hydrated, symbols]);

  const title = useMemo(() => {
    if (!symbols.length) return "Chart Board";
    return `Chart Board (${symbols.length}/${MAX_CHARTS})`;
  }, [symbols.length]);

  const popOutBoard = () => {
    const encoded = encodeURIComponent(symbols.join(","));
    const href = encoded ? `/charts?add=${encoded}` : "/charts";
    window.open(href, "openbb-chart-board", "popup=yes,width=1700,height=1050,resizable=yes,scrollbars=yes");
  };

  return (
    <div className="space-y-4">
      <div className="flex flex-col items-start justify-between gap-2 md:flex-row md:items-center">
        <div>
          <h1 className="text-2xl font-bold">{title}</h1>
          <p className="text-sm text-[#9aaccc]">Ticker clicks open here and stack up to 9 charts.</p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          {RANGES.map((r) => (
            <button key={r} className={`btn ${range === r ? "border-[#3a5ea8]" : ""}`} onClick={() => setRange(r)}>
              {r}
            </button>
          ))}
          <button className="btn" onClick={() => setSymbols([])}>
            Clear All
          </button>
          <button className="btn" onClick={popOutBoard}>
            Pop Out Board
          </button>
        </div>
      </div>

      {!symbols.length ? (
        <div className="card p-4 text-[#9aaccc]">
          No charts yet. Click any ticker from the dashboard tables to add it here.
        </div>
      ) : (
        <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
          {symbols.map((symbol) => (
            <ChartCard key={symbol} symbol={symbol} range={range} onRemove={(target) => setSymbols((prev) => prev.filter((s) => s !== target))} />
          ))}
        </div>
      )}
    </div>
  );
}
