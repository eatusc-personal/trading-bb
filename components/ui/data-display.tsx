"use client";

import { ReactNode } from "react";
import clsx from "clsx";

type PanelHeaderProps = {
  title: string;
  badge?: ReactNode;
};

type NoticeTone = "muted" | "danger" | "warning" | "success";

const toneClass: Record<NoticeTone, string> = {
  muted: "text-[#9aaccc]",
  danger: "text-[#ff6a6a]",
  warning: "text-[#ffca6a]",
  success: "text-[#4fd18b]",
};

export function PanelSection({ children, className }: { children: ReactNode; className?: string }) {
  return <section className={clsx("card p-3", className)}>{children}</section>;
}

export function PanelHeader({ title, badge }: PanelHeaderProps) {
  return (
    <div className="mb-2 flex items-center justify-between gap-2">
      <h2 className="text-sm font-semibold tracking-wide text-[#9aaccc] uppercase">{title}</h2>
      {badge ? <span className="badge">{badge}</span> : null}
    </div>
  );
}

export function InlineNotice({ tone = "muted", children }: { tone?: NoticeTone; children: ReactNode }) {
  return <div className={clsx("text-sm", toneClass[tone])}>{children}</div>;
}

export function CardNotice({ tone = "muted", children }: { tone?: NoticeTone; children: ReactNode }) {
  return <div className={clsx("card p-4", toneClass[tone])}>{children}</div>;
}

export function EmptyTableRow({ colSpan, message }: { colSpan: number; message: string }) {
  return (
    <tr>
      <td colSpan={colSpan} className="text-[#9aaccc]">
        {message}
      </td>
    </tr>
  );
}
