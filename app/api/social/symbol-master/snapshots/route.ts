import { proxy } from "@/lib/proxy";

export async function GET(req: Request) {
  const { searchParams } = new URL(req.url);
  const limit = searchParams.get("limit") || "60";
  return proxy(`/api/social/symbol-master/snapshots?limit=${encodeURIComponent(limit)}`);
}
