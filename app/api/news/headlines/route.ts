import { proxy } from "@/lib/proxy";

export async function GET(req: Request) {
  const { searchParams } = new URL(req.url);
  const limit = searchParams.get("limit");
  if (limit) {
    return proxy(`/api/news/headlines?limit=${encodeURIComponent(limit)}`);
  }
  return proxy("/api/news/headlines");
}
