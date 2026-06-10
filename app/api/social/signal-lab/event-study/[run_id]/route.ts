import { proxy } from "@/lib/proxy";

export async function GET(req: Request, { params }: { params: Promise<{ run_id: string }> }) {
  const { run_id } = await params;
  const { searchParams } = new URL(req.url);
  const horizons = searchParams.get("horizons") || "1,3,5";
  const benchmark = searchParams.get("benchmark") || "SPY";
  const top = searchParams.get("top") || "20";
  return proxy(
    `/api/social/signal-lab/event-study/${encodeURIComponent(run_id)}?horizons=${encodeURIComponent(horizons)}&benchmark=${encodeURIComponent(benchmark)}&top=${encodeURIComponent(top)}`
  );
}
