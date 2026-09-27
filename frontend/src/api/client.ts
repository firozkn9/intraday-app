// Thin fetch wrapper: same-origin cookies, CSRF header on unsafe methods,
// RFC 7807 problem+json errors surfaced as ApiError.

export const API_BASE = "/api/v1";
const UNSAFE = new Set(["POST", "PUT", "PATCH", "DELETE"]);

export interface FieldProblem {
  key: string;
  message: string;
}

export class ApiError extends Error {
  readonly status: number;
  readonly title: string;
  readonly detail?: string;
  readonly errors: FieldProblem[];
  readonly body: Record<string, unknown>;

  constructor(status: number, body: Record<string, unknown>) {
    const title = typeof body.title === "string" ? body.title : `HTTP ${status}`;
    super(title);
    this.status = status;
    this.title = title;
    this.detail = typeof body.detail === "string" ? body.detail : undefined;
    this.errors = Array.isArray(body.errors) ? (body.errors as FieldProblem[]) : [];
    this.body = body;
  }
}

export function readCookie(name: string): string | undefined {
  for (const part of document.cookie.split(";")) {
    const [k, ...v] = part.trim().split("=");
    if (k === name) return decodeURIComponent(v.join("="));
  }
  return undefined;
}

export interface RequestOptions {
  method?: string;
  body?: unknown;
  signal?: AbortSignal;
}

export async function api<T>(path: string, opts: RequestOptions = {}): Promise<T> {
  const method = (opts.method ?? "GET").toUpperCase();
  const headers: Record<string, string> = { Accept: "application/json" };
  if (opts.body !== undefined) headers["Content-Type"] = "application/json";
  if (UNSAFE.has(method)) {
    const csrf = readCookie("csrf");
    if (csrf) headers["X-CSRF-Token"] = csrf;
  }
  const resp = await fetch(`${API_BASE}${path}`, {
    method,
    headers,
    credentials: "same-origin",
    body: opts.body === undefined ? undefined : JSON.stringify(opts.body),
    signal: opts.signal,
  });
  if (resp.status === 204) return undefined as T;
  const type = resp.headers.get("content-type") ?? "";
  const isJson = type.includes("json");
  const payload: unknown = isJson ? await resp.json() : await resp.text();
  if (!resp.ok) {
    throw new ApiError(resp.status, isJson ? (payload as Record<string, unknown>) : { title: String(payload) });
  }
  return payload as T;
}
