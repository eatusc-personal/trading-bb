import { proxy } from "@/lib/proxy";

export async function GET(req: Request) {
  const url = new URL(req.url);
  const limit = url.searchParams.get("limit") || "20";
  const universeSize = url.searchParams.get("universe_size") || "120";
  return proxy(
    `/api/screener/top-volume-triggers?limit=${encodeURIComponent(limit)}&universe_size=${encodeURIComponent(universeSize)}`,
    undefined,
    90000
  );
}
