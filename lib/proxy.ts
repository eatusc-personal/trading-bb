const PY_BASE = process.env.PYTHON_API_BASE || "http://127.0.0.1:8001";
const SOCIAL_API_KEY = process.env.SOCIAL_API_KEY || "";

export async function proxy(path: string, init?: RequestInit, timeoutMs = 30000): Promise<Response> {
  const target = `${PY_BASE}${path}`;
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const res = await fetch(target, {
      ...init,
      signal: controller.signal,
      headers: {
        "Content-Type": "application/json",
        ...(SOCIAL_API_KEY ? { "x-api-key": SOCIAL_API_KEY } : {}),
        ...(init?.headers || {}),
      },
      cache: "no-store",
    });

    const text = await res.text();
    const retryAfter = res.headers.get("retry-after");
    return new Response(text, {
      status: res.status,
      headers: {
        "Content-Type": res.headers.get("content-type") || "application/json",
        ...(retryAfter ? { "Retry-After": retryAfter } : {}),
      },
    });
  } catch (error) {
    if (error instanceof Error && error.name === "AbortError") {
      return Response.json(
        { error: `Python API timed out after ${timeoutMs}ms`, target },
        { status: 504 }
      );
    }
    return Response.json(
      { error: `Python API unavailable at ${PY_BASE}`, detail: String(error) },
      { status: 502 }
    );
  } finally {
    clearTimeout(timeout);
  }
}
