import { proxy } from "@/lib/proxy";

export async function GET(req: Request) {
  const { searchParams } = new URL(req.url);
  const symbol = searchParams.get("symbol") || "";
  const asOf = searchParams.get("as_of");
  const params = new URLSearchParams({ symbol });
  if (asOf) params.set("as_of", asOf);
  return proxy(`/api/social/symbol-master/resolve?${params.toString()}`);
}
