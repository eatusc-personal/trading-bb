import { proxy } from "@/lib/proxy";

export async function GET() {
  return proxy("/api/social/symbol-master/health");
}
