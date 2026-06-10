import { proxy } from "@/lib/proxy";

export async function GET(request: Request) {
  const { searchParams } = new URL(request.url);
  const symbols = (searchParams.get("symbols") || "").trim();
  if (symbols) {
    return proxy(`/api/market/indicators?symbols=${encodeURIComponent(symbols)}`);
  }
  return proxy("/api/market/indicators");
}
