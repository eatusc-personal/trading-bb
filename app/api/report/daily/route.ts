import { proxy } from "@/lib/proxy";

export async function GET(req: Request) {
  const { searchParams } = new URL(req.url);
  const params = new URLSearchParams();
  const watchlist = searchParams.get("watchlist");
  if (watchlist) params.set("watchlist", watchlist);
  if (searchParams.get("refresh") === "true") params.set("refresh", "true");
  const qs = params.toString();
  // First-ever build sweeps every source — give it the long timeout.
  return proxy(`/api/report/daily${qs ? `?${qs}` : ""}`, undefined, 180000);
}
