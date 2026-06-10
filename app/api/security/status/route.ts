import { proxy } from "@/lib/proxy";

export async function GET() {
  return proxy("/api/security/status");
}
