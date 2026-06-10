"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import {
  Area,
  AreaChart,
  Bar,
  BarChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

type Point = {
  date: string;
  open?: number;
  high?: number;
  low?: number;
  close: number;
  volume: number;
  sma20?: number | null;
  sma50?: number | null;
  sma200?: number | null;
};

function clamp(value: number, min: number, max: number) {
  return Math.min(max, Math.max(min, value));
}

function CandlestickChart({ data, showSma, yPanPct = 0 }: { data: Point[]; showSma: boolean; yPanPct?: number }) {
  const containerRef = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(0);
  const [hoverY, setHoverY] = useState<number | null>(null);
  const height = 360;

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
    const valid = data.filter((d) => typeof d.open === "number" && typeof d.high === "number" && typeof d.low === "number");
    if (!valid.length || width < 40) return null;

    const pad = { top: 14, right: 52, bottom: 24, left: 8 };
    const plotW = Math.max(1, width - pad.left - pad.right);
    const plotH = Math.max(1, height - pad.top - pad.bottom);

    const rawMin = Math.min(...valid.map((d) => d.low as number));
    const rawMax = Math.max(...valid.map((d) => d.high as number));
    const rawSpan = Math.max(1e-9, rawMax - rawMin);
    const shift = rawSpan * (yPanPct / 100);
    const min = rawMin + shift;
    const max = rawMax + shift;
    const span = Math.max(1e-9, max - min);
    const toY = (value: number) => pad.top + ((max - value) / span) * plotH;

    const step = plotW / valid.length;
    const candleW = Math.max(2, Math.min(14, step * 0.62));
    const gridTicks = 6;
    const xStride = Math.max(1, Math.ceil(valid.length / 7));

    const smaPath = (key: "sma20" | "sma50" | "sma200") => {
      const points: string[] = [];
      valid.forEach((row, i) => {
        const value = row[key];
        if (value == null || Number.isNaN(value)) return;
        const x = pad.left + step * (i + 0.5);
        const y = toY(value);
        points.push(`${x},${y}`);
      });
      return points.join(" ");
    };

    return {
      valid,
      pad,
      plotW,
      step,
      candleW,
      gridTicks,
      xStride,
      toY,
      max,
      min,
      sma20: smaPath("sma20"),
      sma50: smaPath("sma50"),
      sma200: smaPath("sma200"),
    };
  }, [data, width, yPanPct]);

  return (
    <div ref={containerRef} className="h-[360px] w-full">
      {chart ? (
        <svg
          width={width}
          height={height}
          role="img"
          aria-label="Candlestick price chart"
          onMouseMove={(e) => {
            const rect = e.currentTarget.getBoundingClientRect();
            const y = e.clientY - rect.top;
            const clamped = Math.min(height - chart.pad.bottom, Math.max(chart.pad.top, y));
            setHoverY(clamped);
          }}
          onMouseLeave={() => setHoverY(null)}
        >
          {[...Array(chart.gridTicks)].map((_, i) => {
            const t = i / (chart.gridTicks - 1);
            const y = chart.pad.top + chart.plotW * 0 + (height - chart.pad.bottom - chart.pad.top) * t;
            const value = chart.max - (chart.max - chart.min) * t;
            return (
              <g key={`grid-${i}`}>
                <line x1={chart.pad.left} y1={y} x2={width - chart.pad.right} y2={y} stroke="#253353" strokeDasharray="3 3" />
                <text x={width - chart.pad.right + 6} y={y + 4} fill="#9aaccc" fontSize="11">
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
              <rect x={width - chart.pad.right + 4} y={hoverY - 10} width={48} height={18} rx={3} fill="#0f1930" opacity={0.92} />
              <text x={width - chart.pad.right + 28} y={hoverY + 3} fill="#dce8ff" fontSize="11" textAnchor="middle">
                {(chart.max - ((hoverY - chart.pad.top) / (height - chart.pad.bottom - chart.pad.top)) * (chart.max - chart.min)).toFixed(2)}
              </text>
            </g>
          ) : null}

          {chart.valid.map((row, i) => {
            const x = chart.pad.left + chart.step * (i + 0.5);
            const yHigh = chart.toY(row.high as number);
            const yLow = chart.toY(row.low as number);
            const yOpen = chart.toY(row.open as number);
            const yClose = chart.toY(row.close);
            const top = Math.min(yOpen, yClose);
            const bodyH = Math.max(1.2, Math.abs(yClose - yOpen));
            const up = row.close >= (row.open as number);
            const color = up ? "#4fd18b" : "#ff6a6a";
            return (
              <g key={row.date}>
                <line x1={x} y1={yHigh} x2={x} y2={yLow} stroke={color} strokeWidth={1.1} />
                <rect x={x - chart.candleW / 2} y={top} width={chart.candleW} height={bodyH} fill={color} opacity={0.85} rx={1} />
                {(i % chart.xStride === 0 || i === chart.valid.length - 1) && (
                  <text x={x} y={height - 6} fill="#9aaccc" fontSize="11" textAnchor="middle">
                    {row.date.slice(5)}
                  </text>
                )}
              </g>
            );
          })}

          {showSma && chart.sma20 ? <polyline points={chart.sma20} fill="none" stroke="#ffd166" strokeWidth={1.4} /> : null}
          {showSma && chart.sma50 ? <polyline points={chart.sma50} fill="none" stroke="#4fd18b" strokeWidth={1.4} /> : null}
          {showSma && chart.sma200 ? <polyline points={chart.sma200} fill="none" stroke="#ff6a6a" strokeWidth={1.4} /> : null}
        </svg>
      ) : null}
    </div>
  );
}

export function PriceVolumeChart({ data, showSma = false, interactive = false }: { data: Point[]; showSma?: boolean; interactive?: boolean }) {
  const hasOhlc = data.some((d) => typeof d.open === "number" && typeof d.high === "number" && typeof d.low === "number");
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

  const totalRows = data.length;
  const visibleCount = useMemo(() => clamp(Math.round(totalRows * (zoomPct / 100)), 20, totalRows || 20), [totalRows, zoomPct]);
  const maxOffset = useMemo(() => Math.max(0, totalRows - visibleCount), [totalRows, visibleCount]);
  const visibleData = useMemo(() => {
    if (!data.length) return data;
    const boundedOffset = clamp(offset, 0, maxOffset);
    const start = data.length - visibleCount - boundedOffset;
    const end = start + visibleCount;
    return data.slice(start, end);
  }, [data, offset, visibleCount, maxOffset]);
  const panStep = useMemo(() => Math.max(5, Math.floor((visibleData.length || 20) / 4)), [visibleData.length]);
  const onZoom = (deltaPct: number) => setZoomPct((v) => clamp(v + deltaPct, 20, 100));
  const onPan = (delta: number) => setOffset((v) => clamp(v + delta, 0, maxOffset));

  return (
    <div className="grid gap-3">
      <div className="card p-3">
        {hasOhlc ? (
          <div
            className={interactive ? "touch-none select-none" : ""}
            onWheel={(e) => {
              if (!interactive) return;
              e.preventDefault();
              if (e.shiftKey) {
                onPan(e.deltaY > 0 ? panStep : -panStep);
              } else {
                onZoom(e.deltaY > 0 ? 10 : -10);
              }
            }}
            onPointerDown={(e) => {
              if (!interactive) return;
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
              if (!interactive) return;
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
              if (!interactive) return;
              const drag = dragRef.current;
              if (drag.pointerId === e.pointerId) {
                dragRef.current.active = false;
                dragRef.current.pointerId = null;
              }
            }}
            onPointerCancel={(e) => {
              if (!interactive) return;
              const drag = dragRef.current;
              if (drag.pointerId === e.pointerId) {
                dragRef.current.active = false;
                dragRef.current.pointerId = null;
              }
            }}
            onDoubleClick={() => {
              if (!interactive) return;
              setZoomPct(100);
              setOffset(0);
              setYPanPct(0);
            }}
          >
            {interactive ? (
              <div className="mb-2 flex items-center justify-between text-xs text-[#9aaccc]">
                <span>Zoom {zoomPct}%</span>
                <span>Drag pan + scroll zoom</span>
              </div>
            ) : null}
            <CandlestickChart data={interactive ? visibleData : data} showSma={showSma} yPanPct={interactive ? yPanPct : 0} />
          </div>
        ) : (
          <div className="h-[360px] w-full">
            <ResponsiveContainer>
              <AreaChart data={data}>
                <defs>
                  <linearGradient id="priceFill" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="5%" stopColor="#5ec4ff" stopOpacity={0.35} />
                    <stop offset="95%" stopColor="#5ec4ff" stopOpacity={0.02} />
                  </linearGradient>
                </defs>
                <CartesianGrid stroke="#253353" strokeDasharray="3 3" />
                <XAxis dataKey="date" tick={{ fill: "#9aaccc", fontSize: 12 }} minTickGap={24} />
                <YAxis tick={{ fill: "#9aaccc", fontSize: 12 }} domain={["auto", "auto"]} />
                <Tooltip
                  contentStyle={{ background: "#111a30", border: "1px solid #253353" }}
                  labelStyle={{ color: "#e7edf8" }}
                />
                <Area type="monotone" dataKey="close" stroke="#5ec4ff" fill="url(#priceFill)" strokeWidth={2} />
                {showSma ? <Area type="monotone" dataKey="sma20" stroke="#ffd166" fillOpacity={0} strokeWidth={1.5} /> : null}
                {showSma ? <Area type="monotone" dataKey="sma50" stroke="#4fd18b" fillOpacity={0} strokeWidth={1.5} /> : null}
                {showSma ? <Area type="monotone" dataKey="sma200" stroke="#ff6a6a" fillOpacity={0} strokeWidth={1.5} /> : null}
              </AreaChart>
            </ResponsiveContainer>
          </div>
        )}
      </div>

      <div className="card p-3">
        <div className="h-[160px] w-full">
          <ResponsiveContainer>
            <BarChart data={data}>
              <CartesianGrid stroke="#253353" strokeDasharray="3 3" />
              <XAxis dataKey="date" tick={{ fill: "#9aaccc", fontSize: 12 }} minTickGap={24} />
              <YAxis tick={{ fill: "#9aaccc", fontSize: 12 }} />
              <Tooltip
                contentStyle={{ background: "#111a30", border: "1px solid #253353" }}
                labelStyle={{ color: "#e7edf8" }}
              />
              <Bar dataKey="volume" fill="#3a5ea8" />
            </BarChart>
          </ResponsiveContainer>
        </div>
      </div>
    </div>
  );
}
