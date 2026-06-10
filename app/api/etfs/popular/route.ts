import { proxy } from "@/lib/proxy";

export async function GET(req: Request) {
  const { searchParams } = new URL(req.url);
  const limit = searchParams.get("limit") ?? "250";
  const refresh = searchParams.get("refresh") ?? "false";
  return proxy(`/api/etfs/popular?limit=${encodeURIComponent(limit)}&refresh=${encodeURIComponent(refresh)}`);
}
