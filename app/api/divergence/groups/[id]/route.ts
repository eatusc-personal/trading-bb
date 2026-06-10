import { proxy } from "@/lib/proxy";

type Params = { params: Promise<{ id: string }> };

export async function PUT(req: Request, { params }: Params) {
  const { id } = await params;
  const body = await req.text();
  return proxy(`/api/divergence/groups/${encodeURIComponent(id)}`, { method: "PUT", body });
}

export async function DELETE(_req: Request, { params }: Params) {
  const { id } = await params;
  return proxy(`/api/divergence/groups/${encodeURIComponent(id)}`, { method: "DELETE" });
}
