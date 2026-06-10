import { proxy } from "@/lib/proxy";

export async function POST(req: Request) {
  const body = await req.text();
  return proxy("/api/divergence/scan", { method: "POST", body }, 120000);
}
