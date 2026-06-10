"use client";

import { useMemo, useState } from "react";
import { TickerBoardLink } from "@/components/ticker-board-link";
import { EmptyTableRow, PanelHeader, PanelSection } from "@/components/ui/data-display";
import { formatInteger, formatPercent, formatPrice, pctColorClass } from "@/lib/format";

type Row = {
  symbol: string;
  name?: string;
  price?: number;
  percent_change?: number | null;
  volume?: number;
};

type Props = {
  title: string;
  rows: Row[];
};

type SortKey = "symbol" | "price" | "percent_change" | "volume";

export function MarketTable({ title, rows }: Props) {
  const [sortKey, setSortKey] = useState<SortKey>("percent_change");
  const [desc, setDesc] = useState(true);

  const sorted = useMemo(() => {
    return [...rows].sort((a, b) => {
      const av = a[sortKey] ?? 0;
      const bv = b[sortKey] ?? 0;
      if (typeof av === "string" || typeof bv === "string") {
        return desc ? String(bv).localeCompare(String(av)) : String(av).localeCompare(String(bv));
      }
      return desc ? Number(bv) - Number(av) : Number(av) - Number(bv);
    });
  }, [rows, sortKey, desc]);

  const onSort = (key: SortKey) => {
    if (key === sortKey) {
      setDesc((v) => !v);
    } else {
      setSortKey(key);
      setDesc(true);
    }
  };

  return (
    <PanelSection>
      <PanelHeader title={title} badge={`${rows.length} rows`} />
      <div className="table-wrap">
        <table className="table">
          <thead>
            <tr>
              <th>
                <button className="cursor-pointer" onClick={() => onSort("symbol")}>Ticker</button>
              </th>
              <th>Name</th>
              <th>
                <button className="cursor-pointer" onClick={() => onSort("price")}>Price</button>
              </th>
              <th>
                <button className="cursor-pointer" onClick={() => onSort("percent_change")}>% Chg</button>
              </th>
              <th>
                <button className="cursor-pointer" onClick={() => onSort("volume")}>Volume</button>
              </th>
            </tr>
          </thead>
          <tbody>
            {sorted.length === 0 ? (
              <EmptyTableRow colSpan={5} message="No data" />
            ) : (
              sorted.map((row) => (
                <tr key={`${title}-${row.symbol}`}>
                  <td>
                    <TickerBoardLink className="font-semibold text-[#5ec4ff]" symbol={row.symbol}>
                      {row.symbol}
                    </TickerBoardLink>
                  </td>
                  <td className="max-w-[220px] truncate">{row.name || "-"}</td>
                  <td>{formatPrice(row.price)}</td>
                  <td className={pctColorClass(row.percent_change)}>
                    {formatPercent(row.percent_change)}
                  </td>
                  <td>{formatInteger(row.volume)}</td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>
    </PanelSection>
  );
}
