import { proxy } from "@/lib/proxy";

export async function GET(req: Request, { params }: { params: Promise<{ run_id: string }> }) {
  const { run_id } = await params;
  const { searchParams } = new URL(req.url);
  const limit = searchParams.get("limit") || "50";
  return proxy(`/api/social/signal-lab/run/${encodeURIComponent(run_id)}?limit=${encodeURIComponent(limit)}`);
}
