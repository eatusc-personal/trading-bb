import { proxy } from "@/lib/proxy";

export async function GET(req: Request) {
  const { searchParams } = new URL(req.url);
  const limit = searchParams.get("limit") || "20";
  return proxy(`/api/social/signal-lab/runs?limit=${encodeURIComponent(limit)}`);
}
