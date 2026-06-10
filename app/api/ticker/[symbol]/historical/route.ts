import { proxy } from "@/lib/proxy";

export async function GET(req: Request, { params }: { params: Promise<{ symbol: string }> }) {
  const { symbol } = await params;
  const { searchParams } = new URL(req.url);
  const range = searchParams.get("range") ?? "1Y";
  return proxy(`/api/ticker/${encodeURIComponent(symbol)}/historical?range=${encodeURIComponent(range)}`);
}
