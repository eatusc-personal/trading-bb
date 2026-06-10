import { proxy } from "@/lib/proxy";

export async function POST() {
  return proxy("/api/social/symbol-master/alias/refresh", { method: "POST" });
}
