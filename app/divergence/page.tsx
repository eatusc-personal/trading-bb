"use client";

import { Suspense, useCallback, useEffect, useMemo, useRef, useState } from "react";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { TickerBoardLink } from "@/components/ticker-board-link";
import { apiGet, apiPost } from "@/lib/api";

type PeerGroup = {
  id: string;
  name: string;
  category: string;
  benchmark: string | null;
  members: string[];
  member_count: number;
  notes: string;
};

type GroupsResponse = {
  updated_at: string;
  groups: PeerGroup[];
  category_order: string[];
  defaults: { windows: number[]; z_threshold: number; min_corr: number; max_symbols_per_scan: number };
};

type Signal = {
  window: number;
  z: number;
  ret_pct: number;
  group_ret_pct: number;
  label: string;
  confirmed: boolean;
};

type ScanRow = {
  symbol: string;
  last_close: number | null;
  last_date: string | null;
  returns_pct: Record<string, number | null>;
  z_scores: Record<string, number | null>;
  peer_corr_60d: number | null;
  peer_beta_60d: number | null;
  max_abs_z: number | null;
  signals: Signal[];
  is_diverger: boolean;
  low_correlation: boolean;
};

type ScanSetResult = {
  id: string;
  name: string;
  benchmark: string | null;
  as_of: string | null;
  members_requested: number;
  members_scanned: number;
  fetch_errors: Record<string, string>;
  windows: Record<string, { median_ret_pct: number | null; mean_ret_pct: number | null; benchmark_ret_pct: number | null }>;
  rows: ScanRow[];
  divergers: string[];
  error?: string;
};

type ScanResponse = {
  updated_at: string;
  params: { windows: number[]; z_threshold: number; min_corr: number };
  results: ScanSetResult[];
  diverger_count: number;
  divergers: Array<{ group: string; symbol: string }>;
};

type PulseWindowStats = {
  median_pct: number | null;
  mean_pct: number | null;
  benchmark_pct: number | null;
  best: { symbol: string; pct: number } | null;
  worst: { symbol: string; pct: number } | null;
};

type PulseRow = {
  id: string;
  name: string;
  category: string;
  benchmark: string | null;
  members_total: number;
  members_quoted: number;
  windows: Record<string, PulseWindowStats>;
};

type PulseResponse = {
  updated_at: string;
  as_of: string | null;
  windows: number[];
  symbols_loaded: number;
  symbols_failed: Record<string, string>;
  rows: PulseRow[];
};

type GroupDraft = {
  id: string | null; // null = creating a new group
  name: string;
  category: string;
  benchmark: string;
  members: string;
  notes: string;
};

const Z_OPTIONS = [1.5, 2.0, 2.5, 3.0];
const CORR_OPTIONS = [0.0, 0.2, 0.3, 0.5];
const SELECTED_GROUPS_KEY = "openbb_divergence_groups";
const PULSE_WINDOW_LABELS: Record<string, string> = { "1": "1D", "5": "1W", "21": "1M" };

function pctClass(v: number | null | undefined) {
  if (v == null) return "text-[#9aaccc]";
  if (v > 0) return "text-[#4fd18b]";
  if (v < 0) return "text-[#ff6a6a]";
  return "text-[#9aaccc]";
}

function fmtPct(v: number | null | undefined) {
  if (v == null) return "—";
  return `${v > 0 ? "+" : ""}${v.toFixed(2)}%`;
}

function fmtZ(v: number | null | undefined) {
  if (v == null) return "—";
  return `${v > 0 ? "+" : ""}${v.toFixed(2)}`;
}

export default function DivergencePage() {
  return (
    <Suspense fallback={<div className="card p-4 text-[#9aaccc]">Loading divergence scanner...</div>}>
      <DivergencePageInner />
    </Suspense>
  );
}

function DivergencePageInner() {
  const searchParams = useSearchParams();
  const [groups, setGroups] = useState<PeerGroup[]>([]);
  const [categoryOrder, setCategoryOrder] = useState<string[]>([]);
  const [selected, setSelected] = useState<string[]>([]);
  const [customInput, setCustomInput] = useState("");
  const [customBenchmark, setCustomBenchmark] = useState("");
  const [zThreshold, setZThreshold] = useState(2.0);
  const [minCorr, setMinCorr] = useState(0.3);
  const [scan, setScan] = useState<ScanResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const [pulse, setPulse] = useState<PulseResponse | null>(null);
  const [pulseLoading, setPulseLoading] = useState(false);
  const [pulseError, setPulseError] = useState<string | null>(null);
  const [pulseSortWindow, setPulseSortWindow] = useState<string>("1");
  const [pulseSortDesc, setPulseSortDesc] = useState(true);

  const [draft, setDraft] = useState<GroupDraft | null>(null);
  const [draftSaving, setDraftSaving] = useState(false);
  const [draftError, setDraftError] = useState<string | null>(null);
  const [confirmDelete, setConfirmDelete] = useState(false);

  const initializedRef = useRef(false);

  const loadGroups = useCallback(async () => {
    try {
      const res = await apiGet<GroupsResponse>("/api/divergence/groups");
      setGroups(res.groups);
      setCategoryOrder(res.category_order || []);
    } catch (e) {
      setError(String(e));
    }
  }, []);

  const runScanWith = useCallback(
    async (input: { groups: string[]; symbols: string[]; benchmark: string | null }) => {
      if (!input.groups.length && input.symbols.length < 3) {
        setError("Pick at least one peer group, or enter 3+ custom symbols.");
        return;
      }
      setLoading(true);
      setError(null);
      try {
        const res = await apiPost<ScanResponse>("/api/divergence/scan", {
          groups: input.groups,
          symbols: input.symbols,
          benchmark: input.benchmark,
          z_threshold: zThreshold,
          min_corr: minCorr,
        });
        setScan(res);
      } catch (e) {
        setError(String(e));
      } finally {
        setLoading(false);
      }
    },
    [zThreshold, minCorr]
  );

  const loadPulse = useCallback(async () => {
    setPulseLoading(true);
    setPulseError(null);
    try {
      const res = await apiGet<PulseResponse>("/api/divergence/overview", { timeoutMs: 300000 });
      setPulse(res);
    } catch (e) {
      setPulseError(String(e));
    } finally {
      setPulseLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadGroups();
  }, [loadGroups]);

  // Initialize selection from URL params (deep links from Market/Watchlist/ETF tabs)
  // or fall back to the last-used groups in localStorage. Auto-scan on deep links.
  useEffect(() => {
    if (initializedRef.current) return;
    initializedRef.current = true;

    const paramGroups = (searchParams.get("groups") || "")
      .split(",")
      .map((s) => s.trim().toLowerCase())
      .filter(Boolean);
    const paramSymbols = (searchParams.get("symbols") || "")
      .split(",")
      .map((s) => s.trim().toUpperCase())
      .filter((s) => /^[A-Z0-9.\-]{1,12}$/.test(s));
    const paramBenchmark = (searchParams.get("benchmark") || "").trim().toUpperCase();

    if (paramGroups.length || paramSymbols.length >= 3) {
      setSelected(paramGroups);
      if (paramSymbols.length >= 3) setCustomInput(paramSymbols.join(", "));
      if (paramBenchmark) setCustomBenchmark(paramBenchmark);
      void runScanWith({ groups: paramGroups, symbols: paramSymbols, benchmark: paramBenchmark || null });
      window.history.replaceState({}, "", "/divergence");
      return;
    }

    try {
      const raw = localStorage.getItem(SELECTED_GROUPS_KEY);
      const parsed = raw ? (JSON.parse(raw) as string[]) : [];
      if (Array.isArray(parsed) && parsed.length) setSelected(parsed.filter((s) => typeof s === "string"));
      else setSelected(["semiconductors"]);
    } catch {
      setSelected(["semiconductors"]);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [searchParams]);

  const toggleGroup = (id: string) => {
    setSelected((prev) => {
      const next = prev.includes(id) ? prev.filter((g) => g !== id) : [...prev, id];
      localStorage.setItem(SELECTED_GROUPS_KEY, JSON.stringify(next));
      return next;
    });
  };

  const customSymbols = useMemo(
    () =>
      customInput
        .split(/[\s,;]+/)
        .map((s) => s.trim().toUpperCase())
        .filter((s) => /^[A-Z0-9.\-]{1,12}$/.test(s)),
    [customInput]
  );

  const runScan = useCallback(async () => {
    await runScanWith({
      groups: selected,
      symbols: customSymbols,
      benchmark: customBenchmark.trim().toUpperCase() || null,
    });
  }, [runScanWith, selected, customSymbols, customBenchmark]);

  const scanSingleGroup = useCallback(
    (id: string) => {
      setSelected([id]);
      localStorage.setItem(SELECTED_GROUPS_KEY, JSON.stringify([id]));
      void runScanWith({ groups: [id], symbols: [], benchmark: null });
    },
    [runScanWith]
  );

  const groupedByCategory = useMemo(() => {
    const order = categoryOrder.length ? categoryOrder : ["Industries", "Custom"];
    const buckets = new Map<string, PeerGroup[]>();
    for (const g of groups) {
      const cat = g.category || "Custom";
      if (!buckets.has(cat)) buckets.set(cat, []);
      buckets.get(cat)!.push(g);
    }
    const known = order.filter((c) => buckets.has(c));
    const unknown = Array.from(buckets.keys()).filter((c) => !order.includes(c));
    return [...known, ...unknown].map((cat) => ({ category: cat, groups: buckets.get(cat)! }));
  }, [groups, categoryOrder]);

  const sortedPulseRows = useMemo(() => {
    if (!pulse) return [];
    const rows = [...pulse.rows];
    rows.sort((a, b) => {
      const av = a.windows[pulseSortWindow]?.median_pct;
      const bv = b.windows[pulseSortWindow]?.median_pct;
      const an = av == null ? Number.NEGATIVE_INFINITY : av;
      const bn = bv == null ? Number.NEGATIVE_INFINITY : bv;
      return pulseSortDesc ? bn - an : an - bn;
    });
    return rows;
  }, [pulse, pulseSortWindow, pulseSortDesc]);

  const openEditor = (group: PeerGroup | null) => {
    setDraftError(null);
    setConfirmDelete(false);
    if (group) {
      setDraft({
        id: group.id,
        name: group.name,
        category: group.category,
        benchmark: group.benchmark || "",
        members: group.members.join(", "),
        notes: group.notes || "",
      });
    } else {
      setDraft({ id: null, name: "", category: "My Buckets", benchmark: "", members: "", notes: "" });
    }
  };

  const saveDraft = async () => {
    if (!draft) return;
    const members = draft.members
      .split(/[\s,;]+/)
      .map((s) => s.trim().toUpperCase())
      .filter((s) => /^[A-Z0-9.\-]{1,12}$/.test(s));
    if (!draft.name.trim()) {
      setDraftError("Name is required.");
      return;
    }
    if (members.length < 3) {
      setDraftError("A group needs at least 3 valid symbols.");
      return;
    }
    setDraftSaving(true);
    setDraftError(null);
    const payload = {
      name: draft.name.trim(),
      category: draft.category.trim() || "Custom",
      benchmark: draft.benchmark.trim().toUpperCase() || null,
      members,
      notes: draft.notes.trim(),
    };
    try {
      if (draft.id) {
        const res = await fetch(`/api/divergence/groups/${encodeURIComponent(draft.id)}`, {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
        });
        if (!res.ok) throw new Error((await res.json()).detail || `Save failed: ${res.status}`);
      } else {
        await apiPost("/api/divergence/groups", payload);
      }
      setDraft(null);
      await loadGroups();
      if (pulse) void loadPulse();
    } catch (e) {
      setDraftError(String(e));
    } finally {
      setDraftSaving(false);
    }
  };

  const deleteDraft = async () => {
    if (!draft?.id) return;
    if (!confirmDelete) {
      setConfirmDelete(true);
      return;
    }
    setDraftSaving(true);
    setDraftError(null);
    try {
      const res = await fetch(`/api/divergence/groups/${encodeURIComponent(draft.id)}`, { method: "DELETE" });
      if (!res.ok) throw new Error((await res.json()).detail || `Delete failed: ${res.status}`);
      setSelected((prev) => prev.filter((id) => id !== draft.id));
      setDraft(null);
      await loadGroups();
      if (pulse) void loadPulse();
    } catch (e) {
      setDraftError(String(e));
    } finally {
      setDraftSaving(false);
    }
  };

  return (
    <div className="space-y-4">
      <div>
        <h1 className="text-2xl font-bold">Divergence Scanner</h1>
        <p className="text-sm text-[#9aaccc]">
          Finds stocks that are NOT moving with their peers — e.g. the whole industry crashed but two names held up.
          A signal needs |z| ≥ threshold vs the group AND a normal 60-day correlation with the group, so habitual
          independent movers don&apos;t trigger false alarms.
        </p>
      </div>

      <section className="card p-3">
        <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
          <div>
            <h2 className="text-sm font-semibold tracking-wide text-[#9aaccc] uppercase">Group Pulse</h2>
            <p className="text-xs text-[#9aaccc]">
              One click shows every group&apos;s gain/loss over 1 day / 1 week / 1 month — no need to scan each bucket.
              {pulse?.as_of ? ` As of ${pulse.as_of}.` : ""}
            </p>
          </div>
          <button className="btn border-[#3a5ea8]" onClick={() => void loadPulse()} disabled={pulseLoading}>
            {pulseLoading ? "Scanning all groups..." : pulse ? "Refresh Pulse" : "Scan All Groups"}
          </button>
        </div>
        {pulseError ? <div className="text-sm text-[#ff6a6a]">{pulseError}</div> : null}
        {pulseLoading && !pulse ? (
          <div className="text-sm text-[#9aaccc]">Fetching history for all groups — first run can take ~30s, after that it&apos;s cached.</div>
        ) : null}
        {pulse ? (
          <div className="table-wrap">
            <table className="table">
              <thead>
                <tr>
                  <th>Group</th>
                  <th>Category</th>
                  {pulse.windows.map((w) => (
                    <th key={`pw-${w}`}>
                      <button
                        className="cursor-pointer"
                        onClick={() => {
                          const key = String(w);
                          if (pulseSortWindow === key) setPulseSortDesc((v) => !v);
                          else {
                            setPulseSortWindow(key);
                            setPulseSortDesc(true);
                          }
                        }}
                      >
                        {PULSE_WINDOW_LABELS[String(w)] || `${w}D`}
                        {pulseSortWindow === String(w) ? (pulseSortDesc ? " ↓" : " ↑") : ""}
                      </button>
                    </th>
                  ))}
                  <th>Benchmark</th>
                  <th>Best (1D)</th>
                  <th>Worst (1D)</th>
                  <th></th>
                </tr>
              </thead>
              <tbody>
                {sortedPulseRows.map((row) => {
                  const primary = row.windows[pulseSortWindow];
                  const w1 = row.windows["1"];
                  return (
                    <tr key={`pulse-${row.id}`} className={selected.includes(row.id) ? "bg-[#142445]/40" : ""}>
                      <td>
                        <button
                          className="cursor-pointer font-semibold text-[#5ec4ff] hover:underline"
                          title="Run a full divergence scan for this group"
                          onClick={() => scanSingleGroup(row.id)}
                        >
                          {row.name}
                        </button>
                        {row.members_quoted < row.members_total ? (
                          <span className="ml-1 text-xs text-[#ffca6a]" title="Some members had no price data">
                            {row.members_quoted}/{row.members_total}
                          </span>
                        ) : null}
                      </td>
                      <td className="text-xs text-[#9aaccc]">{row.category}</td>
                      {pulse.windows.map((w) => {
                        const stats = row.windows[String(w)];
                        return (
                          <td key={`pv-${row.id}-${w}`} className={`font-semibold ${pctClass(stats?.median_pct)}`}>
                            {fmtPct(stats?.median_pct)}
                          </td>
                        );
                      })}
                      <td className={`text-xs ${pctClass(primary?.benchmark_pct)}`}>
                        {row.benchmark ? `${row.benchmark} ${fmtPct(primary?.benchmark_pct)}` : "—"}
                      </td>
                      <td className="text-xs">
                        {w1?.best ? (
                          <Link href={`/ticker/${w1.best.symbol}`} className="text-[#4fd18b] hover:underline">
                            {w1.best.symbol} {fmtPct(w1.best.pct)}
                          </Link>
                        ) : (
                          "—"
                        )}
                      </td>
                      <td className="text-xs">
                        {w1?.worst ? (
                          <Link href={`/ticker/${w1.worst.symbol}`} className="text-[#ff6a6a] hover:underline">
                            {w1.worst.symbol} {fmtPct(w1.worst.pct)}
                          </Link>
                        ) : (
                          "—"
                        )}
                      </td>
                      <td>
                        <button className="btn px-2 py-0.5 text-xs" onClick={() => scanSingleGroup(row.id)}>
                          Scan
                        </button>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        ) : null}
      </section>

      <section className="card space-y-3 p-3">
        <div className="flex items-center justify-between">
          <h2 className="text-sm font-semibold tracking-wide text-[#9aaccc] uppercase">Peer Groups</h2>
          <button className="btn px-3 py-1.5 text-xs" onClick={() => openEditor(null)}>
            + New Group
          </button>
        </div>
        {groupedByCategory.map(({ category, groups: catGroups }) => (
          <div key={category}>
            <div className="mb-1 text-xs font-semibold tracking-wide text-[#7587ad] uppercase">{category}</div>
            <div className="flex flex-wrap gap-2">
              {catGroups.map((g) => (
                <span key={g.id} className="inline-flex items-stretch">
                  <button
                    className={`btn rounded-r-none px-3 py-1.5 ${selected.includes(g.id) ? "border-[#3a5ea8] bg-[#142445] text-white" : ""}`}
                    onClick={() => toggleGroup(g.id)}
                    title={`${g.members.join(", ")}${g.benchmark ? ` vs ${g.benchmark}` : ""}${g.notes ? `\n${g.notes}` : ""}`}
                  >
                    {g.name} <span className="text-xs text-[#9aaccc]">({g.member_count})</span>
                  </button>
                  <button
                    className="btn rounded-l-none border-l-0 px-2 py-1.5 text-xs text-[#9aaccc] hover:text-white"
                    title={`Edit ${g.name} (members, benchmark, notes)`}
                    onClick={() => openEditor(g)}
                  >
                    ✎
                  </button>
                </span>
              ))}
            </div>
          </div>
        ))}

        <div className="grid gap-3 md:grid-cols-2">
          <div>
            <h2 className="mb-2 text-sm font-semibold tracking-wide text-[#9aaccc] uppercase">
              Custom Set <span className="normal-case">(stocks or ETFs, 3+ symbols)</span>
            </h2>
            <input
              className="input"
              value={customInput}
              onChange={(e) => setCustomInput(e.target.value.toUpperCase())}
              placeholder="e.g. NVDA, AMD, AVGO, MU, INTC  or  XLK, XLF, XLE, XLV"
            />
            {customSymbols.length > 0 && customSymbols.length < 3 ? (
              <div className="mt-1 text-xs text-[#ffca6a]">Need at least 3 symbols to compare.</div>
            ) : null}
          </div>
          <div className="grid grid-cols-3 gap-3">
            <div>
              <h2 className="mb-2 text-sm font-semibold tracking-wide text-[#9aaccc] uppercase">Benchmark</h2>
              <input
                className="input"
                value={customBenchmark}
                onChange={(e) => setCustomBenchmark(e.target.value.toUpperCase())}
                placeholder="optional, e.g. SMH"
              />
            </div>
            <div>
              <h2 className="mb-2 text-sm font-semibold tracking-wide text-[#9aaccc] uppercase">Z Threshold</h2>
              <select className="input" value={zThreshold} onChange={(e) => setZThreshold(Number(e.target.value))}>
                {Z_OPTIONS.map((z) => (
                  <option key={z} value={z}>
                    {z.toFixed(1)}σ
                  </option>
                ))}
              </select>
            </div>
            <div>
              <h2 className="mb-2 text-sm font-semibold tracking-wide text-[#9aaccc] uppercase">Min Corr</h2>
              <select className="input" value={minCorr} onChange={(e) => setMinCorr(Number(e.target.value))}>
                {CORR_OPTIONS.map((c) => (
                  <option key={c} value={c}>
                    {c.toFixed(1)}
                  </option>
                ))}
              </select>
            </div>
          </div>
        </div>

        <div className="flex items-center gap-3">
          <button className="btn border-[#3a5ea8] bg-[#142445] text-white" onClick={() => void runScan()} disabled={loading}>
            {loading ? "Scanning..." : "Scan for Divergence"}
          </button>
          <span className="text-xs text-[#9aaccc]">
            {selected.length} group{selected.length === 1 ? "" : "s"}
            {customSymbols.length >= 3 ? ` + custom set (${customSymbols.length})` : ""} · windows 1D / 5D / 20D
          </span>
        </div>
      </section>

      {error ? <div className="card p-4 text-[#ff6a6a]">{error}</div> : null}
      {loading ? <div className="card p-4 text-[#9aaccc]">Fetching history and computing peer statistics...</div> : null}

      {scan && !loading ? (
        <>
          <section className="card p-3">
            <div className="mb-2 flex items-center justify-between">
              <h2 className="text-sm font-semibold tracking-wide text-[#9aaccc] uppercase">Confirmed Divergers</h2>
              <span className="badge">{scan.diverger_count} flagged</span>
            </div>
            {scan.diverger_count === 0 ? (
              <div className="text-sm text-[#9aaccc]">
                No confirmed divergence at |z| ≥ {scan.params.z_threshold}σ. Everything is moving with its group.
              </div>
            ) : (
              <div className="flex flex-wrap gap-2">
                {scan.divergers.map((d) => (
                  <Link key={`${d.group}-${d.symbol}`} href={`/ticker/${d.symbol}`} className="badge px-2 py-1 text-[#ffca6a] hover:text-white">
                    {d.symbol} <span className="text-[#9aaccc]">· {d.group}</span>
                  </Link>
                ))}
              </div>
            )}
          </section>

          {scan.results.map((set) => (
            <section key={set.id} className="card p-3">
              <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
                <div>
                  <h2 className="text-base font-semibold">{set.name}</h2>
                  <div className="text-xs text-[#9aaccc]">
                    as of {set.as_of ?? "—"} · {set.members_scanned}/{set.members_requested} members
                    {set.benchmark ? ` · benchmark ${set.benchmark}` : ""}
                  </div>
                </div>
                <div className="flex flex-wrap gap-2">
                  {Object.entries(set.windows).map(([w, stats]) => (
                    <span key={w} className="badge px-2 py-1 text-xs">
                      {w}D group <span className={pctClass(stats.median_ret_pct)}>{fmtPct(stats.median_ret_pct)}</span>
                      {stats.benchmark_ret_pct != null ? (
                        <>
                          {" "}· {set.benchmark} <span className={pctClass(stats.benchmark_ret_pct)}>{fmtPct(stats.benchmark_ret_pct)}</span>
                        </>
                      ) : null}
                    </span>
                  ))}
                </div>
              </div>

              {set.error ? <div className="text-sm text-[#ff6a6a]">{set.error}</div> : null}
              {Object.keys(set.fetch_errors || {}).length ? (
                <div className="mb-2 text-xs text-[#ffca6a]">
                  No data: {Object.keys(set.fetch_errors).join(", ")}
                </div>
              ) : null}

              {set.rows.length ? (
                <div className="overflow-x-auto">
                  <table className="table w-full text-sm">
                    <thead>
                      <tr className="text-left text-xs text-[#9aaccc] uppercase">
                        <th className="p-2">Symbol</th>
                        <th className="p-2 text-right">Close</th>
                        <th className="p-2 text-right">1D</th>
                        <th className="p-2 text-right">5D</th>
                        <th className="p-2 text-right">20D</th>
                        <th className="p-2 text-right">z 1D</th>
                        <th className="p-2 text-right">z 5D</th>
                        <th className="p-2 text-right">z 20D</th>
                        <th className="p-2 text-right">Corr</th>
                        <th className="p-2 text-right">Beta</th>
                        <th className="p-2">Signal</th>
                      </tr>
                    </thead>
                    <tbody>
                      {set.rows.map((row) => {
                        const strongest = row.signals.length
                          ? row.signals.reduce((a, b) => (Math.abs(a.z) >= Math.abs(b.z) ? a : b))
                          : null;
                        return (
                          <tr
                            key={row.symbol}
                            className={`border-t border-[#253353] ${row.is_diverger ? "bg-[#142445]/60" : ""}`}
                          >
                            <td className="p-2">
                              <Link href={`/ticker/${row.symbol}`} className="font-semibold text-[#5ec4ff] hover:underline">
                                {row.symbol}
                              </Link>{" "}
                              <TickerBoardLink className="text-xs text-[#9aaccc] hover:text-white" symbol={row.symbol}>
                                chart
                              </TickerBoardLink>
                            </td>
                            <td className="p-2 text-right">{row.last_close ?? "—"}</td>
                            {["1", "5", "20"].map((w) => (
                              <td key={`r${w}`} className={`p-2 text-right ${pctClass(row.returns_pct[w])}`}>
                                {fmtPct(row.returns_pct[w])}
                              </td>
                            ))}
                            {["1", "5", "20"].map((w) => {
                              const z = row.z_scores[w];
                              const hot = z != null && Math.abs(z) >= scan.params.z_threshold;
                              return (
                                <td
                                  key={`z${w}`}
                                  className={`p-2 text-right ${hot ? "font-bold " + (z! > 0 ? "text-[#4fd18b]" : "text-[#ff6a6a]") : "text-[#9aaccc]"}`}
                                >
                                  {fmtZ(z)}
                                </td>
                              );
                            })}
                            <td className={`p-2 text-right ${row.low_correlation ? "text-[#ffca6a]" : "text-[#9aaccc]"}`}>
                              {row.peer_corr_60d ?? "—"}
                            </td>
                            <td className="p-2 text-right text-[#9aaccc]">{row.peer_beta_60d ?? "—"}</td>
                            <td className="p-2">
                              {strongest ? (
                                <span
                                  className={`badge px-2 py-0.5 text-xs ${
                                    strongest.confirmed ? (strongest.z > 0 ? "text-[#4fd18b]" : "text-[#ff6a6a]") : "text-[#9aaccc]"
                                  }`}
                                  title={
                                    strongest.confirmed
                                      ? `${strongest.window}D: ${fmtPct(strongest.ret_pct)} vs group ${fmtPct(strongest.group_ret_pct)}`
                                      : "Outlier move, but this name normally has low correlation with the group"
                                  }
                                >
                                  {strongest.window}D · {strongest.label}
                                  {strongest.confirmed ? "" : " (low corr)"}
                                </span>
                              ) : row.low_correlation ? (
                                <span className="text-xs text-[#9aaccc]">independent mover</span>
                              ) : (
                                <span className="text-xs text-[#9aaccc]">tracking group</span>
                              )}
                            </td>
                          </tr>
                        );
                      })}
                    </tbody>
                  </table>
                </div>
              ) : null}
            </section>
          ))}

          <section className="card p-3 text-xs text-[#9aaccc]">
            <span className="font-semibold text-[#d9e6ff]">How to read this:</span> z-scores compare each stock&apos;s return
            against the median/MAD of its peers (leave-one-out, so a big outlier can&apos;t hide itself). Corr/Beta are computed
            over the last 60 trading days vs an equal-weight index of the other members. A row is only a confirmed diverger
            when the move is extreme (|z| ≥ {scan.params.z_threshold}) AND the name normally tracks the group
            (corr ≥ {scan.params.min_corr}). Green = held up / led; red = fell harder / lagged.
          </section>
        </>
      ) : null}

      {draft ? (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4" onClick={() => setDraft(null)}>
          <div className="card w-full max-w-xl p-4" onClick={(e) => e.stopPropagation()}>
            <div className="mb-3 flex items-start justify-between">
              <div className="text-lg font-semibold">{draft.id ? `Edit Group: ${draft.name || draft.id}` : "New Group"}</div>
              <button className="btn px-2 py-1" onClick={() => setDraft(null)}>
                Close
              </button>
            </div>
            <div className="space-y-3">
              <div className="grid gap-3 md:grid-cols-3">
                <div className="md:col-span-2">
                  <div className="kpi-label mb-1">Name</div>
                  <input className="input" value={draft.name} onChange={(e) => setDraft({ ...draft, name: e.target.value })} />
                </div>
                <div>
                  <div className="kpi-label mb-1">Benchmark (optional)</div>
                  <input
                    className="input"
                    value={draft.benchmark}
                    onChange={(e) => setDraft({ ...draft, benchmark: e.target.value.toUpperCase() })}
                    placeholder="e.g. SMH"
                  />
                </div>
              </div>
              <div>
                <div className="kpi-label mb-1">Category</div>
                <input
                  className="input"
                  value={draft.category}
                  onChange={(e) => setDraft({ ...draft, category: e.target.value })}
                  placeholder="Industries / Crypto / My Buckets / ..."
                  list="divergence-categories"
                />
                <datalist id="divergence-categories">
                  {categoryOrder.map((c) => (
                    <option key={c} value={c} />
                  ))}
                </datalist>
              </div>
              <div>
                <div className="kpi-label mb-1">Members (3+ symbols, comma or space separated)</div>
                <textarea
                  className="input min-h-[90px]"
                  value={draft.members}
                  onChange={(e) => setDraft({ ...draft, members: e.target.value.toUpperCase() })}
                  placeholder="MARA, RIOT, CLSK, CORZ, ..."
                />
              </div>
              <div>
                <div className="kpi-label mb-1">Notes (optional)</div>
                <input className="input" value={draft.notes} onChange={(e) => setDraft({ ...draft, notes: e.target.value })} />
              </div>
              {draftError ? <div className="text-sm text-[#ff6a6a]">{draftError}</div> : null}
              <div className="flex items-center gap-2">
                <button className="btn border-[#3a5ea8] bg-[#142445] text-white" onClick={() => void saveDraft()} disabled={draftSaving}>
                  {draftSaving ? "Saving..." : "Save"}
                </button>
                {draft.id ? (
                  <button className="btn text-[#ff6a6a]" onClick={() => void deleteDraft()} disabled={draftSaving}>
                    {confirmDelete ? "Click again to confirm delete" : "Delete Group"}
                  </button>
                ) : null}
                <span className="spacer flex-1" />
                <span className="text-xs text-[#9aaccc]">Saved to python_api/peer_groups.json</span>
              </div>
            </div>
          </div>
        </div>
      ) : null}
    </div>
  );
}
