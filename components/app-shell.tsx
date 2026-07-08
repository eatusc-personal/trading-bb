"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { Activity, BarChart3, ChartCandlestick, FileText, GitCompareArrows, ListFilter, MessagesSquare, Newspaper, Search, Star, Tags } from "lucide-react";
import { FormEvent, useState } from "react";
import clsx from "clsx";

const navItems = [
  { href: "/", label: "Market", icon: BarChart3 },
  { href: "/report", label: "Daily Report", icon: FileText },
  { href: "/charts", label: "Chart Board", icon: ChartCandlestick },
  { href: "/news", label: "Headline News", icon: Newspaper },
  { href: "/technical", label: "Technical", icon: Activity },
  { href: "/screener", label: "Screener", icon: ListFilter },
  { href: "/divergence", label: "Divergence", icon: GitCompareArrows },
  { href: "/social", label: "Social Sentiment", icon: MessagesSquare },
  { href: "/watchlist", label: "Watchlist", icon: Star },
  { href: "/etfs", label: "Top ETFs", icon: Tags },
];

export function AppShell({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const router = useRouter();
  const [symbol, setSymbol] = useState("");

  const onSubmit = (e: FormEvent) => {
    e.preventDefault();
    const value = symbol.trim().toUpperCase();
    if (!value) return;
    router.push(`/charts?add=${encodeURIComponent(value)}`);
    setSymbol("");
  };

  return (
    <div className="min-h-screen text-foreground">
      <div className="mx-auto flex max-w-[1600px]">
        <aside className="hidden w-64 shrink-0 border-r border-[#253353] p-4 md:block">
          <div className="mb-6 flex items-center gap-2">
            <ChartCandlestick className="text-[#5ec4ff]" size={20} />
            <div>
              <div className="text-sm text-[#9aaccc]">Market research</div>
              <div className="text-lg font-semibold">Trading Dashboard BB</div>
            </div>
          </div>
          <nav className="space-y-2">
            {navItems.map((item) => {
              const Icon = item.icon;
              const active = pathname === item.href;
              return (
                <Link
                  key={item.href}
                  href={item.href}
                  className={clsx(
                    "flex items-center gap-3 rounded-lg border px-3 py-2 text-sm",
                    active
                      ? "border-[#3a5ea8] bg-[#142445] text-white"
                      : "border-[#253353] bg-[#111a30] text-[#9aaccc] hover:text-white"
                  )}
                >
                  <Icon size={16} />
                  {item.label}
                </Link>
              );
            })}
          </nav>
        </aside>

        <main className="flex min-h-screen flex-1 flex-col p-3 md:p-6">
          <header className="mb-4 flex flex-col gap-3 md:flex-row md:items-center md:justify-between">
            <form onSubmit={onSubmit} className="flex w-full max-w-xl items-center gap-2">
              <div className="relative w-full">
                <Search size={16} className="pointer-events-none absolute top-1/2 left-3 -translate-y-1/2 text-[#9aaccc]" />
                <input
                  value={symbol}
                  onChange={(e) => setSymbol(e.target.value)}
                  placeholder="Search ticker (AAPL, MSFT, NVDA...)"
                  className="input pl-9"
                />
              </div>
              <button type="submit" className="btn">
                Go
              </button>
            </form>
            <button className="btn" onClick={() => router.refresh()}>
              Refresh
            </button>
          </header>
          {children}
        </main>
      </div>
    </div>
  );
}
