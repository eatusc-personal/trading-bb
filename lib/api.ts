type RequestOptions = {
  timeoutMs?: number;
};

export async function apiGet<T>(path: string, options?: RequestOptions): Promise<T> {
  const controller = options?.timeoutMs ? new AbortController() : undefined;
  const timeout = options?.timeoutMs ? setTimeout(() => controller?.abort(), options.timeoutMs) : null;
  let res: Response;
  try {
    res = await fetch(path, { cache: "no-store", signal: controller?.signal });
  } catch (error) {
    if (error instanceof Error && error.name === "AbortError") {
      throw new Error(`Request timed out after ${options?.timeoutMs}ms`);
    }
    throw error;
  } finally {
    if (timeout) clearTimeout(timeout);
  }
  if (!res.ok) {
    const msg = await safeText(res);
    throw new Error(msg || `Request failed: ${res.status}`);
  }
  return res.json();
}

export async function apiPost<T>(path: string, body: unknown): Promise<T> {
  const res = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    cache: "no-store",
  });
  if (!res.ok) {
    const msg = await safeText(res);
    throw new Error(msg || `Request failed: ${res.status}`);
  }
  return res.json();
}

async function safeText(res: Response) {
  try {
    const j = await res.json();
    return j.detail || j.error || JSON.stringify(j);
  } catch {
    try {
      return await res.text();
    } catch {
      return "";
    }
  }
}
