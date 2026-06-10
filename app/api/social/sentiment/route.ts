import { proxy } from "@/lib/proxy";

export async function GET(req: Request) {
  const { searchParams } = new URL(req.url);
  const limit = searchParams.get("limit");
  const experimentId = searchParams.get("experiment_id");
  const refresh = searchParams.get("refresh");
  const params = new URLSearchParams();
  if (limit) params.set("limit", limit);
  if (experimentId) params.set("experiment_id", experimentId);
  if (refresh) params.set("refresh", refresh);
  const query = params.toString();
  // The backend serves its last completed scan instantly (refreshes happen in a
  // background thread), so this generous timeout is just belt-and-braces.
  return proxy(query ? `/api/social/sentiment?${query}` : "/api/social/sentiment", undefined, 60000);
}
