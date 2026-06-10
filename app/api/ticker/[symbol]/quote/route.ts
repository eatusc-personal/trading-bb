import { proxy } from "@/lib/proxy";

export async function GET(_req: Request, { params }: { params: Promise<{ symbol: string }> }) {
  const { symbol } = await params;
  return proxy(`/api/ticker/${encodeURIComponent(symbol)}/quote`);
}
