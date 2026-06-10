export function formatDateTime(value?: string | null): string {
  if (!value) return "-";
  return new Date(value).toLocaleString();
}

export function formatPrice(value?: number | null): string {
  if (value == null || Number.isNaN(value)) return "-";
  return value.toFixed(2);
}

export function formatPercent(value?: number | null): string {
  if (value == null || Number.isNaN(value)) return "-";
  return `${value.toFixed(2)}%`;
}

export function formatInteger(value?: number | null): string {
  if (value == null || Number.isNaN(value)) return "-";
  return Intl.NumberFormat().format(value);
}

export function pctColorClass(value?: number | null): string {
  return Number(value || 0) >= 0 ? "text-[#4fd18b]" : "text-[#ff6a6a]";
}
