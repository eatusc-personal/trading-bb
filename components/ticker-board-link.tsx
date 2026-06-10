"use client";

import Link from "next/link";
import { ReactNode } from "react";

type Props = {
  symbol: string;
  className?: string;
  children?: ReactNode;
};

export function TickerBoardLink({ symbol, className, children }: Props) {
  const normalized = symbol.trim().toUpperCase();
  const href = `/charts?add=${encodeURIComponent(normalized)}`;

  return (
    <Link href={href} className={className}>
      {children ?? normalized}
    </Link>
  );
}
