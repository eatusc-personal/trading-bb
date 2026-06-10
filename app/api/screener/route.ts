import { proxy } from "@/lib/proxy";

export async function POST(req: Request) {
  const body = await req.text();
  return proxy("/api/screener", { method: "POST", body }, 90000);
}
