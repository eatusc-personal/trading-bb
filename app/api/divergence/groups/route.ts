import { proxy } from "@/lib/proxy";

export async function GET() {
  return proxy("/api/divergence/groups");
}

export async function POST(req: Request) {
  const body = await req.text();
  return proxy("/api/divergence/groups", { method: "POST", body });
}
