import { proxy } from "@/lib/proxy";

export async function GET(req: Request) {
  const { searchParams } = new URL(req.url);
  const symbols = searchParams.get("symbols") ?? "";
  return proxy(`/api/watchlist/quote?symbols=${encodeURIComponent(symbols)}`);
}
