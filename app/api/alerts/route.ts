import { proxy } from "@/lib/proxy";

export async function GET(req: Request) {
  const { searchParams } = new URL(req.url);
  const symbols = searchParams.get("symbols");
  if (symbols) {
    return proxy(`/api/alerts?symbols=${encodeURIComponent(symbols)}`);
  }
  return proxy("/api/alerts");
}
